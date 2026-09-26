"""分析 agent 轨迹：把 Claude Code 的行为映射到方案 0.1 节的四类结构性问题。

  python -m eval.analyze pilot-v2                     # 分析整个 run：逐题报告 + 横向对比
  python -m eval.analyze <trial 目录>                  # 只分析一道题（<run>/<benchmark>/<id>/<k>）
  python -m eval.analyze pilot-v2 --allow-test        # 评测集默认拒绝（机制设计阶段不看评测集轨迹）

输出：每道题写 analysis.md / analysis.json 到 trial 目录；整个 run 的对比表写到 <run>/analysis.md。

数据来源（优先级从高到低）：
  pier/agent/<trial>/agent/sessions/projects/**/*.jsonl   Claude Code 会话文件（带时间戳）
  pier/agent/<trial>/agent/claude-code.txt                 stream-json 输出（无时间戳，时间类指标为空）

指标与方案的对应：
  提前完成   宣布完成但未通过；结束时 TodoWrite 仍有未完成项；参考解文件覆盖率（仅调试集）
  计划漂移   上下文压缩次数；压缩后重读的文件
  脏状态     同一测试失败签名反复出现；同一文件反复修改
  上下文污染 上下文增长；探索占比；最长"既不改代码也不跑测试"的连续步数
  打转       方案 7.4：最近 10 次工具调用中同一动作签名出现 ≥ 3 次
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from eval.config import DEFAULT_RUNS, load_yaml, resolve_path

# ---------------------------------------------------------------- 分类规则
EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
READ_TOOLS = {"Read", "NotebookRead"}
SEARCH_TOOLS = {"Grep", "Glob", "LS"}
PLAN_TOOLS = {"TodoWrite", "Task", "Agent", "ExitPlanMode"}

TEST_RE = re.compile(r"\b(pytest|py\.test|tox|nox|go test|cargo (test|nextest)|(npm|pnpm|yarn)\b[^|;&]*\btest\b|"
                     r"vitest|jest|mocha|mvn\b[^|;&]*\btest|gradle\w*\b[^|;&]*\btest|ctest|make\b[^|;&]*\btest|unittest)")
BUILD_RE = re.compile(r"\b(cargo (build|check|clippy)|go (build|vet)|tsc\b|(npm|pnpm|yarn)\b[^|;&]*\b(build|install|i)\b|"
                      r"mvn\b|gradle|cmake|make\b|pip3? install|uv (pip|sync)|poetry install)")
EXPLORE_RE = re.compile(r"^(grep|rg|ag|find|ls|cat|head|tail|sed -n|awk|wc|tree|less|file|stat|du|which|"
                        r"git (log|show|diff|status|blame|grep|ls-files))\b")
EDIT_BASH_RE = re.compile(r"(sed -i|perl -pi|\btee\b|(^|[^>2&])>\s*[\w./-]+\.\w+|\bpatch\b|git apply|git checkout --|"
                          r"write_text\(|\.write\(|open\([^)]*['\"]w)")
GIT_WRITE_RE = re.compile(r"\bgit (add|commit|stash|reset|checkout|restore|rebase|merge|cherry-pick)\b")
TEST_PATH_RE = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*\.py$|_test\.(py|go)$|"
                          r"\.(test|spec)\.[jt]sx?$|(^|/)conftest\.py$")
CLAIM_RE = re.compile(r"(^\s*(all )?done\b|\ball \d+ [\w-]+(?: [\w-]+)? (items|changes|features|requirements)[^.\n]{0,40}"
                      r"(implemented|complete|done|in place)|\ball (changes|items|tests|features|requirements|release.note|\w+ changes)[^.\n]{0,60}"
                      r"(implemented|complete|done|pass|verified|in place)|(implementation|task|work) is complete|"
                      r"successfully (implemented|completed)|in place and verified)", re.I)
WAIT_RE = re.compile(r"(^|[;&|]\s*)sleep\s+\d+")
# 测试输出中的失败信号（管道 `| tail` 会吞掉退出码，因此不能只看 is_error）
TEST_SUMMARY_RE = re.compile(r"\b\d+ (passed|failed)\b|\bTests?:\s+\d+|test result:|^(ok|FAIL|PASS)\s|--- (PASS|FAIL):|"
                             r"Test Files\s+\d+|\b\d+ (passing|failing)\b", re.M)
TEST_FAIL_RE = re.compile(r"\b[1-9]\d* (failed|errors?)\b|^(FAILED|ERROR)\s+\S+|--- FAIL:|^FAIL\s|"
                          r"Tests?:\s+[^\n]*\b[1-9]\d* failed|\b[1-9]\d* failing\b|test result: FAILED", re.M)
GIT_PROBE_RE = re.compile(r"git\s+(cat-file\s+--batch-all-objects|fsck|reflog|log\s[^|;&]*--all|rev-list\s[^|;&]*--all|"
                          r"for-each-ref|show-ref|verify-pack|count-objects)|\.git/(objects|packed-refs|refs|logs)")
# 寻找答案的途径（方案 8.4 统计"作弊尝试"）；只记录尝试，是否得逞需人工核实输出
SEEK_RULES = [
    ("网络请求", re.compile(r"\b(curl|wget|git (clone|fetch|pull)|pip3? download|pip3? install\s+[\w.-]+==|"
                            r"npm (view|pack|info)|pnpm view|yarn info|go get|cargo (search|download)|gh )\b")),
    ("搜索仓库外的文件系统", re.compile(r"\b(grep|rg|find|locate)\b[^|;&]*\s(/|/usr|/opt|/root|/home|/var|/srv|/tmp|"
                                     r"\S*site-packages\S*|\S*node_modules\S*|~)(\s|/|$)")),
    ("查看 harness 目录", re.compile(r"(^|\s|['\"])/(logs|tests|solution|installed-agent|oracle)(/|\s|$|['\"])")),
    ("git 历史 / 对象库", GIT_PROBE_RE),
]
NON_CODE_RE = re.compile(r"(^|/)(docs?|\.github)/|\.(md|rst|txt|svg|png|jpe?g|gif|ico)$|(^|/)(README|CHANGELOG|LICENSE)[^/]*$", re.I)
FAIL_NAME_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)|^--- FAIL: (\S+)|^\s*(?:✗|×|FAIL)\s+(.{5,120})$", re.M)

CATS = ["探索", "编辑", "测试", "构建/安装", "等待", "计划", "其他"]


def strip_cd(cmd: str) -> str:
    cmd = cmd.strip()
    while True:
        m = re.match(r"^cd\s+\S+\s*(&&|;)\s*", cmd)
        if not m:
            return cmd
        cmd = cmd[m.end():]


def classify(name: str, inp: dict) -> str:
    if name in EDIT_TOOLS:
        return "编辑"
    if name in READ_TOOLS or name in SEARCH_TOOLS or name in {"WebFetch", "WebSearch"}:
        return "探索"
    if name in PLAN_TOOLS:
        return "计划"
    if name == "Bash":
        cmd = strip_cd(str(inp.get("command", "")))
        if WAIT_RE.search(cmd):
            return "等待"
        if TEST_RE.search(cmd):
            return "测试"
        if EDIT_BASH_RE.search(cmd):
            return "编辑"
        if BUILD_RE.search(cmd):
            return "构建/安装"
        if EXPLORE_RE.match(cmd):
            return "探索"
    return "其他"


def signature(name: str, inp: dict) -> str:
    """动作签名：工具名 + 归一化后的关键参数。"""
    for k in ("command", "file_path", "pattern", "path"):
        if k in inp:
            v = re.sub(r"\s+", " ", str(inp[k])).strip()
            if name in EDIT_TOOLS:        # 编辑同一文件的不同位置不算重复动作
                v += "|" + str(hash(str(inp.get("old_string", ""))[:200]))
            return f"{name}:{v[:300]}"
    return f"{name}:{json.dumps(inp, sort_keys=True, ensure_ascii=False)[:300]}"


def short(s, n=90) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def ts(v) -> dt.datetime | None:
    if not v:
        return None
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def result_text(content) -> str:
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    return str(content or "")


# ---------------------------------------------------------------- 读取轨迹
@dataclass
class Call:
    idx: int
    id: str
    name: str
    inp: dict
    cat: str
    t_call: dt.datetime | None
    turn: int
    sidechain: bool
    t_result: dt.datetime | None = None
    is_error: bool = False
    output: str = ""
    after_compaction: int = 0


@dataclass
class Trace:
    source: str
    calls: list[Call] = field(default_factory=list)
    turns: list[dict] = field(default_factory=list)          # 主线程每条模型回复：ts、上下文大小、输出 token
    compactions: list[dict] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)            # 主线程的文字回复
    t_first: dt.datetime | None = None
    t_last: dt.datetime | None = None
    user_before_turn: list[dt.datetime | None] = field(default_factory=list)


def load_events(agent_dir: Path) -> tuple[list[dict], str]:
    sess = sorted((agent_dir / "sessions" / "projects").rglob("*.jsonl")) if (agent_dir / "sessions").exists() else []
    events = []
    for f in sess:
        sub = "subagents" in f.parts
        for line in f.read_text(errors="replace").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if sub:
                e["isSidechain"] = True
            events.append(e)
    if events:
        events.sort(key=lambda e: e.get("timestamp") or "")
        return events, "会话文件（含时间戳）"
    cc = agent_dir / "claude-code.txt"
    if cc.exists():
        for line in cc.read_text(errors="replace").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events, "stream-json（无时间戳）"
    return [], "无"


def build_trace(events: list[dict], source: str) -> Trace:
    tr = Trace(source=source)
    by_id: dict[str, Call] = {}
    seen_msg: dict[str, int] = {}
    last_user_ts = None
    n_compact = 0
    for e in events:
        t = ts(e.get("timestamp"))
        if t:
            tr.t_first = tr.t_first or t
            tr.t_last = t
        side = bool(e.get("isSidechain"))
        typ = e.get("type")
        if typ == "system" and e.get("subtype") == "compact_boundary":
            n_compact += 1
            meta = e.get("compactMetadata") or e.get("compact_metadata") or {}
            tr.compactions.append({"turn": len(tr.turns), "call": len(tr.calls), "pre_tokens": meta.get("preTokens"),
                                   "trigger": meta.get("trigger")})
            continue
        msg = e.get("message") or {}
        if typ == "assistant":
            mid = msg.get("id") or e.get("uuid") or f"_{len(seen_msg)}"
            u = msg.get("usage") or {}
            ctx = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
            if not side:
                if mid not in seen_msg:
                    seen_msg[mid] = len(tr.turns)
                    tr.turns.append({"ts": t, "ctx": ctx, "out": u.get("output_tokens", 0), "prev_user_ts": last_user_ts})
                else:
                    tr.turns[seen_msg[mid]].update(ctx=max(ctx, tr.turns[seen_msg[mid]]["ctx"]),
                                                   out=max(u.get("output_tokens", 0), tr.turns[seen_msg[mid]]["out"]))
            turn_idx = seen_msg.get(mid, len(tr.turns)) if not side else -1
            for c in msg.get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_use":
                    inp = c.get("input") or {}
                    call = Call(len(tr.calls), c.get("id", ""), c.get("name", "?"), inp, classify(c.get("name", "?"), inp),
                                t, turn_idx, side, after_compaction=n_compact)
                    tr.calls.append(call)
                    by_id[call.id] = call
                elif c.get("type") == "text" and not side and c.get("text", "").strip():
                    tr.texts.append(c["text"])
        elif typ == "user":
            last_user_ts = t or last_user_ts
            content = msg.get("content")
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("tool_use_id") in by_id:
                        call = by_id[c["tool_use_id"]]
                        call.t_result = t
                        call.is_error = bool(c.get("is_error"))
                        call.output = result_text(c.get("content"))[:20000]
        elif typ == "result" and e.get("result"):
            tr.texts.append(e["result"])
    return tr


# ---------------------------------------------------------------- 指标
def gold_files(task_dir: Path) -> list[str] | None:
    for rel in ("solution/gold.patch", "solution/solution.patch"):
        p = task_dir / rel
        if p.exists():
            return sorted(set(re.findall(r"^diff --git a/(\S+) b/", p.read_text(errors="ignore"), re.M)))
    return None


def analyze_trial(d: Path, task_dir: Path | None, split: str | None) -> dict:
    run, grade = _read(d / "run.json"), _read(d / "grade.json")
    agent_dirs = sorted((d / "pier" / "agent").glob("*/agent"))
    events, source = load_events(agent_dirs[0]) if agent_dirs else ([], "无")
    tr = build_trace(events, source)
    main = [c for c in tr.calls if not c.sidechain]
    side = [c for c in tr.calls if c.sidechain]
    for pos, c in enumerate(main):          # 位置按主线程重新编号，子 agent 的调用不占步数
        c.idx = pos
    m: dict = {"trial": f"{d.parent.parent.name}/{d.parent.name}#{d.name}", "source": source}

    # 结果
    resolved = grade.get("resolved") if grade else run.get("inline_resolved")
    m.update(resolved=resolved, fix_rate=grade.get("fix_rate"), exception=run.get("exception"))
    rw = grade.get("rewards") or {}
    if "f2p_success" in rw:
        m.update(f2p=f"{rw['f2p_success']}/{rw['f2p_success'] + rw['f2p_failure']}", p2p_regressions=rw.get("p2p_failure"))
    elif "f2p_total" in rw:
        m.update(f2p=f"{rw.get('f2p_passed')}/{rw['f2p_total']}",
                 p2p_regressions=rw.get("p2p_total", 0) - rw.get("p2p_passed", 0))

    # 规模与时间
    m["turns"] = len(tr.turns)
    m["tool_calls"] = len(main)
    m["subagent_calls"] = len(side)
    m["wall_min"] = round((tr.t_last - tr.t_first).total_seconds() / 60, 1) if tr.t_first and tr.t_last else None
    tool_sec = sum((c.t_result - c.t_call).total_seconds() for c in main if c.t_call and c.t_result)
    model_sec = sum((t["ts"] - t["prev_user_ts"]).total_seconds() for t in tr.turns
                    if t["ts"] and t["prev_user_ts"] and t["ts"] > t["prev_user_ts"])
    m["tool_min"] = round(tool_sec / 60, 1) if tr.t_first else None
    wait_sec = sum((c.t_result - c.t_call).total_seconds() for c in main if c.cat == "等待" and c.t_call and c.t_result)
    m["wait_min"] = round(wait_sec / 60, 1) if tr.t_first else None
    m["tool_names"] = dict(Counter(c.name for c in main).most_common(12))
    m["subagent_tool_names"] = dict(Counter(c.name for c in side).most_common(6))
    seeks = []
    for c in main + side:
        kind = None
        if c.name in ("WebFetch", "WebSearch"):
            kind = "网络请求"
            what = c.inp.get("url") or c.inp.get("query")
        elif c.name == "Bash":
            what = str(c.inp.get("command", ""))
            kind = next((k for k, rx in SEEK_RULES if rx.search(what)), None)
        elif c.name in READ_TOOLS and re.match(r"/(logs|tests|solution)/", str(c.inp.get("file_path", ""))):
            kind, what = "查看 harness 目录", c.inp.get("file_path")
        if kind:
            seeks.append({"kind": kind, "step": c.idx, "sidechain": c.sidechain, "cmd": short(what, 160),
                          "output": short(c.output, 200), "error": c.is_error})
    m["answer_seeking"] = seeks[:20]
    m["answer_seeking_count"] = dict(Counter(x["kind"] for x in seeks))
    m["model_min"] = round(model_sec / 60, 1) if tr.t_first else None
    slow = sorted((c for c in main if c.t_call and c.t_result), key=lambda c: c.t_result - c.t_call, reverse=True)[:5]
    m["slowest_calls"] = [{"sec": round((c.t_result - c.t_call).total_seconds()), "cat": c.cat,
                           "what": short(c.inp.get("command") or c.inp.get("file_path") or c.name)} for c in slow]

    # 行为构成与阶段
    cat_count = Counter(c.cat for c in main)
    m["category_share"] = {k: round(cat_count[k] / max(1, len(main)), 3) for k in CATS}
    phases = []
    for b in range(10):
        seg = main[b * len(main) // 10:(b + 1) * len(main) // 10]
        cc = Counter(c.cat for c in seg)
        phases.append({k: cc[k] for k in CATS if cc[k]})
    m["phases"] = phases
    first_edit = next((c.idx for c in main if c.cat == "编辑"), None)
    m["first_edit_at"] = f"{first_edit}/{len(main)}" if first_edit is not None else "从未编辑"
    streak = best = 0
    best_end = None
    for c in main:
        streak = 0 if c.cat in ("编辑", "测试") else streak + 1
        if streak > best:
            best, best_end = streak, c.idx
    m["longest_no_progress"] = best
    m["longest_no_progress_at"] = f"{best_end - best + 1}–{best_end}" if best_end is not None else None

    # 编辑
    edited = Counter()
    for c in main:
        if c.name in EDIT_TOOLS and c.inp.get("file_path"):
            edited[c.inp["file_path"]] += 1
    m["files_edited"] = len(edited)
    m["edit_calls"] = sum(edited.values())
    m["churn_files"] = [f"{short(f, 70)}（{n} 次）" for f, n in edited.most_common(5) if n >= 5]
    m["edit_errors"] = sum(1 for c in main if c.name in EDIT_TOOLS and c.is_error)

    # 阅读与压缩
    reads = Counter(c.inp.get("file_path") for c in main if c.name in READ_TOOLS and c.inp.get("file_path"))
    m["files_read"] = len(reads)
    m["reread_3plus"] = sum(1 for n in reads.values() if n >= 3)
    m["compactions"] = len(tr.compactions)
    m["compaction_at"] = [f"第 {x['turn']} 轮（压缩前 {x['pre_tokens'] or '?'} tokens）" for x in tr.compactions]
    if tr.compactions:
        before = {c.inp.get("file_path") for c in main if c.name in READ_TOOLS and c.after_compaction == 0}
        after = {c.inp.get("file_path") for c in main if c.name in READ_TOOLS and c.after_compaction > 0}
        m["reread_after_compaction"] = len(before & after)
    ctx = [t["ctx"] for t in tr.turns if t["ctx"]]
    if ctx:
        m["ctx_max_k"] = round(max(ctx) / 1000)
        m["ctx_curve_k"] = [round(ctx[min(len(ctx) - 1, i * len(ctx) // 10)] / 1000) for i in range(10)] + [round(ctx[-1] / 1000)]

    # 测试与失败签名
    tests = [c for c in main if c.cat == "测试"]
    failed = lambda c: c.is_error or bool(TEST_FAIL_RE.search(c.output))
    m["test_runs"] = len(tests)
    m["test_runs_failed"] = sum(1 for c in tests if failed(c))
    # 后台运行后再 tail / cat 日志查看结果：按输出中的测试摘要识别
    observed = [c for c in main if c.name == "Bash" and c.cat != "测试" and TEST_SUMMARY_RE.search(c.output)]
    m["test_results_viewed_separately"] = len(observed)
    m["test_results_viewed_failed"] = sum(1 for c in observed if TEST_FAIL_RE.search(c.output))
    fail_views = [c for c in tests + observed if failed(c)]
    m["last_failure_seen_at"] = f"{max(c.idx for c in fail_views)}/{len(main)}" if fail_views else None
    m["last_test_at"] = f"{tests[-1].idx}/{len(main)}" if tests else "从未运行测试"
    sigs = Counter()
    for c in tests + observed:
        if failed(c):
            names = sorted({next(x for x in g if x) for g in FAIL_NAME_RE.findall(c.output)})[:5]
            if names:
                sigs[tuple(names)] += 1
    m["repeated_failure"] = [f"{n} 次：{short(', '.join(s), 100)}" for s, n in sigs.most_common(3) if n >= 3]

    # 打转（方案 7.4）
    episodes, i = [], 0
    while i < len(main):
        window = main[max(0, i - 9): i + 1]
        cnt = Counter(signature(c.name, c.inp) for c in window)
        sig, n = cnt.most_common(1)[0] if cnt else ("", 0)
        if n >= 3:
            episodes.append(f"第 {main[i].idx} 步附近：{short(sig, 100)}（10 步内 {n} 次）")
            i += 10
        else:
            i += 1
    m["stuck_episodes"] = episodes[:5]
    m["stuck_count"] = len(episodes)
    m["tool_errors"] = sum(1 for c in main if c.is_error)

    # 计划与完成声明
    todo_calls = [c for c in main if c.name == "TodoWrite"]
    m["todowrite_calls"] = len(todo_calls)
    if todo_calls:
        todos = todo_calls[-1].inp.get("todos") or []
        st = Counter(t.get("status") for t in todos)
        m["todos_final"] = dict(st)
        m["todos_unfinished"] = [short(t.get("content"), 80) for t in todos if t.get("status") != "completed"][:8]
    final = tr.texts[-1] if tr.texts else ""
    m["final_message"] = short(final, 400)
    m["claims_done"] = bool(CLAIM_RE.search(final))
    m["false_completion"] = m["claims_done"] and resolved is False

    # 补丁与参考解覆盖（仅调试集）
    patch = d / "patch.diff"
    agent_files = sorted(set(re.findall(r"^diff --git a/(\S+) b/", patch.read_text(errors="ignore"), re.M))) if patch.exists() else []
    m["patch_files"] = len(agent_files)
    m["patch_test_files"] = [f for f in agent_files if TEST_PATH_RE.search(f)]
    if split == "dev" and task_dir is not None:
        gold = gold_files(task_dir)
        if gold:
            code = lambda f: not TEST_PATH_RE.search(f) and not NON_CODE_RE.search(f)
            g = {f for f in gold if code(f)}
            a = {f for f in agent_files if code(f)}
            m["gold_src_files"] = len(g)
            m["gold_recall"] = round(len(g & a) / len(g), 2) if g else None
            m["gold_missed"] = sorted(g - a)[:15]
            m["extra_files"] = len(a - g)
    return m


def _read(p: Path) -> dict:
    return json.loads(p.read_text()) if p.exists() else {}


# ---------------------------------------------------------------- 输出
def bar(d: dict) -> str:
    total = sum(d.values()) or 1
    return " ".join(f"{k}{round(v / total * 100)}%" for k, v in sorted(d.items(), key=lambda x: -x[1]))


def trial_md(m: dict) -> str:
    res = {True: "✅ 通过", False: "❌ 未通过", None: "⚠️ 无结果"}[m.get("resolved")]
    L = [f"## {m['trial']}", "",
         f"结果：{res}" + (f"；F2P {m['f2p']}" if m.get("f2p") else "") +
         (f"；P2P 回归 {m['p2p_regressions']}" if m.get("p2p_regressions") is not None else "") +
         (f"；Fix Rate {m['fix_rate']}" if m.get("fix_rate") is not None else "") +
         (f"；异常 {m['exception']}" if m.get("exception") else "") + f"。数据来源：{m['source']}。", "",
         "### 提前完成", "",
         f"- 宣布完成：{'是' if m['claims_done'] else '否'}" + ("，**但未通过评测（false completion）**" if m["false_completion"] else ""),
         f"- TodoWrite：调用 {m['todowrite_calls']} 次" + (f"，结束时状态 {m.get('todos_final')}" if m.get("todos_final") else "")]
    if m.get("todos_unfinished"):
        L.append(f"- 结束时仍未完成的条目：{'；'.join(m['todos_unfinished'])}")
    if "gold_recall" in m:
        L.append(f"- 参考解源码文件覆盖率：{m['gold_recall']:.0%}（参考解 {m['gold_src_files']} 个，agent 额外改了 {m['extra_files']} 个）")
        if m["gold_missed"]:
            L.append(f"- 漏改的文件：{'、'.join(m['gold_missed'])}")
    L += [f"- 最后一次跑测试：第 {m['last_test_at']} 步" +
          (f"；最后一次在测试结果中看到失败：第 {m['last_failure_seen_at']} 步" if m.get("last_failure_seen_at") else ""),
          "", "### 计划漂移", "",
          f"- 上下文压缩：{m['compactions']} 次" + (f"（{'；'.join(m['compaction_at'])}）" if m["compaction_at"] else ""),
          f"- 上下文峰值：{m.get('ctx_max_k', '?')}k tokens；走势（每 10% 轮次）：{' → '.join(map(str, m.get('ctx_curve_k', [])))}k"]
    if "reread_after_compaction" in m:
        L.append(f"- 压缩后重新读取压缩前读过的文件：{m['reread_after_compaction']} 个")
    L += ["", "### 脏状态 / 打转", "",
          f"- 测试运行 {m['test_runs']} 次，其中输出含失败 {m['test_runs_failed']} 次；"
          f"另有 {m['test_results_viewed_separately']} 次单独查看测试结果（后台运行后读日志），其中含失败 {m['test_results_viewed_failed']} 次",
          f"- 同一失败签名重复出现 ≥ 3 次：{'；'.join(m['repeated_failure']) or '无'}",
          f"- 反复修改的文件（≥ 5 次）：{'；'.join(m['churn_files']) or '无'}；编辑报错 {m['edit_errors']} 次",
          f"- 打转片段（10 步内同一动作 ≥ 3 次）：{m['stuck_count']} 处" + (f"：{'；'.join(m['stuck_episodes'][:3])}" if m["stuck_episodes"] else ""),
          f"- 工具报错合计 {m['tool_errors']} 次", "", "### 上下文污染", "",
          f"- 行为构成：{bar({k: v for k, v in m['category_share'].items() if v})}",
          f"- 第一次编辑出现在第 {m['first_edit_at']} 步",
          f"- 最长连续 {m['longest_no_progress']} 步既未编辑也未测试（第 {m['longest_no_progress_at']} 步）",
          f"- 读过 {m['files_read']} 个文件，其中 {m['reread_3plus']} 个读了 3 次以上", "",
          "阶段变化（按工具调用顺序分成 10 段）：", ""]
    for i, p in enumerate(m["phases"]):
        L.append(f"  {i * 10:>3}–{i * 10 + 10}%：{bar(p) if p else '—'}")
    L += ["", "### 规划与完整性", "",
          f"- 工具调用分布：{'，'.join(f'{k} {v}' for k, v in m['tool_names'].items())}"]
    if m.get("subagent_tool_names"):
        L.append(f"- 子 agent 工具调用：{'，'.join(f'{k} {v}' for k, v in m['subagent_tool_names'].items())}")
    if m.get("answer_seeking"):
        L.append(f"- **寻找答案的尝试 {len(m['answer_seeking'])} 次**（{'，'.join(f'{k} {v}' for k, v in m['answer_seeking_count'].items())}）；"
                 "是否得逞需核实输出：")
        for g in m["answer_seeking"][:12]:
            L.append(f"  - [{g['kind']}] 第 {g['step']} 步{'（子 agent）' if g['sidechain'] else ''}：`{g['cmd']}` → "
                     f"{'报错：' if g['error'] else ''}{g['output'] or '（无输出）'}")
    else:
        L.append("- 未发现寻找答案的尝试（网络、仓库外文件、harness 目录、git 历史）")
    L += ["", "### 时间与规模", "",
          f"- {m['turns']} 轮、{m['tool_calls']} 次工具调用" + (f"、子 agent 工具调用 {m['subagent_calls']} 次" if m["subagent_calls"] else "") +
          (f"；会话 {m['wall_min']} min，其中工具执行 {m['tool_min']} min（含 sleep 等待 {m['wait_min']} min）、模型生成 {m['model_min']} min" if m.get("wall_min") else ""),
          f"- 补丁 {m['patch_files']} 个文件" + (f"，其中测试文件：{'、'.join(m['patch_test_files'])}" if m["patch_test_files"] else "")]
    if m["slowest_calls"] and m["slowest_calls"][0]["sec"]:
        L.append("- 最慢的工具调用：" + "；".join(f"{x['sec']}s [{x['cat']}] {x['what']}" for x in m["slowest_calls"][:3]))
    L += ["", "### 最后的自我总结", "", f"> {m['final_message']}", ""]
    return "\n".join(L)


def compare_md(ms: list[dict]) -> str:
    cols = [("结果", lambda m: {True: "✅", False: "❌", None: "⚠️"}[m.get("resolved")]),
            ("F2P / 回归", lambda m: f"{m.get('f2p', '')} / {m.get('p2p_regressions', '')}" if m.get("f2p") else ""),
            ("宣布完成", lambda m: "是" + ("（假）" if m["false_completion"] else "") if m["claims_done"] else "否"),
            ("参考解覆盖", lambda m: f"{m['gold_recall']:.0%}" if m.get("gold_recall") is not None else "—"),
            ("轮数", lambda m: m["turns"]), ("工具调用", lambda m: m["tool_calls"]),
            ("探索 / 编辑 / 测试 %", lambda m: "/".join(f"{round(m['category_share'][k] * 100)}" for k in ("探索", "编辑", "测试"))),
            ("首次编辑", lambda m: m["first_edit_at"]), ("最长无进展", lambda m: m["longest_no_progress"]),
            ("测试（含失败）", lambda m: f"{m['test_runs']}（{m['test_runs_failed'] + m['test_results_viewed_failed']}）"),
            ("找答案尝试", lambda m: len(m.get("answer_seeking") or [])),
            ("压缩", lambda m: m["compactions"]), ("上下文峰值", lambda m: f"{m.get('ctx_max_k', '?')}k"),
            ("打转", lambda m: m["stuck_count"]), ("重复失败", lambda m: len(m["repeated_failure"])),
            ("反复修改文件", lambda m: len(m["churn_files"])),
            ("工具（等待）/ 模型 (min)", lambda m: f"{m.get('tool_min')}（{m.get('wait_min')}）/ {m.get('model_min')}" if m.get("tool_min") is not None else "")]
    L = ["| 题目 | " + " | ".join(c for c, _ in cols) + " |", "|---|" + "---|" * len(cols)]
    for m in ms:
        L.append(f"| {m['trial']} | " + " | ".join(str(f(m)) for _, f in cols) + " |")
    return "\n".join(L)


# ---------------------------------------------------------------- 入口
def results_root() -> Path:
    return resolve_path(DEFAULT_RUNS.parent, load_yaml(DEFAULT_RUNS)["results_root"])


def tasks_root() -> Path:
    return resolve_path(DEFAULT_RUNS.parent, load_yaml(DEFAULT_RUNS)["task_dirs"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eval.analyze")
    ap.add_argument("target", help="run_id、run 目录或单个 trial 目录")
    ap.add_argument("--allow-test", action="store_true", help="允许分析评测集的运行（机制设计阶段请勿使用）")
    a = ap.parse_args(argv)

    p = Path(a.target)
    root = p if p.is_dir() else results_root() / a.target
    trials = [root] if (root / "run.json").exists() else sorted(x.parent for x in root.glob("*/*/*/run.json"))
    if not trials:
        sys.exit(f"{root} 下没有找到 trial")
    run_root = trials[0].parents[2]
    split = _read(run_root / "plan.json").get("split")
    if split == "test" and not a.allow_test:
        sys.exit("这是评测集的运行。机制设计阶段不查看评测集轨迹；确需分析请加 --allow-test。")

    ms = []
    for d in trials:
        m = analyze_trial(d, tasks_root() / d.parent.parent.name / d.parent.name, split)
        md = trial_md(m)
        (d / "analysis.md").write_text(md, encoding="utf-8")
        (d / "analysis.json").write_text(json.dumps(m, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        ms.append(m)
        print(md)
    if len(ms) > 1:
        cmp = compare_md(ms)
        (run_root / "analysis.md").write_text(f"# {run_root.name} 轨迹分析\n\n{cmp}\n\n" +
                                              "\n".join(trial_md(m) for m in ms), encoding="utf-8")
        print("\n# 横向对比\n\n" + cmp)
        print(f"\n已写入 {run_root / 'analysis.md'} 及每题目录下的 analysis.md / analysis.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
