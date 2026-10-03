"""稳定点有多少：只读 Belay 运行的事件日志与 worker 会话轨迹，找出“改完了、能合并”的时刻。

不改任何代码与文件。放在 belay/ 目录下运行（需要项目的 venv）：
  python stable_points.py v9-conan v9-spot-improve-2
  python stable_points.py v9-spot-improve-2 --test-re "simulate (run|score)"   # LHTB：任务自带的验证命令也算自测
  python stable_points.py v9-conan --lines 80 --quiet-min 3 --min-gap 5 --cap 20

两种稳定点（只看 worker 自己的会话轨迹，不需要模型）：
  自测通过   bash 跑了测试命令（pytest 等，或 --test-re 匹配的命令）且退出码为 0，并且上次稳定点之后改过文件
  编辑停顿   一阵改文件之后，至少 --quiet-min 分钟没再成功改文件，期间 worker 还在调用别的工具（读、跑命令）
每个稳定点取当时最新的快照，看它相对当时链头未合并多少（文件数、增删行数），以及这棵树后来的命运
（合并 / 被回归门拦下 / 复核不批准 / 从没请求过）。

最后按候选规则回放一遍：稳定点上未合并 ≥ --lines 行（或 ≥ --files 个文件）就发后台请求；两次之间至少 --min-gap 分钟；
链头之后有未合并改动、但距上次合并（或上次请求）超过 --cap 分钟时，不看改动量也发。只估计“会在什么时候发”，
不知道回归门与复核者会不会放行（这棵树真被请求过时标出当时的结局）。
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Optional

from belay.runtime.session import TEST_CMD
from eval.belay_report import find_targets, load_graph


def mmss(sec: Optional[float]) -> str:
    if sec is None:
        return "-"
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def table(head: list[str], rows: list[list]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    return out + ["| " + " | ".join(str(x) for x in r) + " |" for r in rows]


EXIT = re.compile(r"\[exit code (-?\d+)")


def tool_calls(bdir: Path) -> list[dict]:
    """会话轨迹里的工具调用：时间、工具名、bash 命令、是否出错、退出码。"""
    calls = []
    for path in sorted((bdir / "sessions").glob("S*.jsonl"), key=lambda p: int(re.sub(r"\D", "", p.stem) or 0)):
        pending: dict[str, dict] = {}
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("type") == "message":
                m = r.get("message") or {}
                if m.get("role") == "assistant" and isinstance(m.get("content"), list):
                    for b in m["content"]:
                        if b.get("type") == "tool_use":
                            pending[b.get("id")] = b.get("input") or {}
            elif r.get("type") == "tool_result":
                for x in r.get("results") or []:
                    inp = pending.pop(x.get("id"), {})
                    out = str(x.get("output") or "")
                    m = EXIT.search(out[-300:])
                    calls.append({"t": r.get("t"), "session": path.stem, "name": x.get("name"),
                                  "cmd": str(inp.get("command") or "") if x.get("name") == "bash" else "",
                                  "error": bool(x.get("error")),
                                  "rc": int(m.group(1)) if m else (None if x.get("error") else 0)})
    return sorted(calls, key=lambda c: c["t"] or 0)


def analyse(bdir: Path, a) -> list[str]:
    g, events = load_graph(bdir)
    if g.run is None or not events:
        return [f"# {bdir}", "", "（事件日志是空的）"]
    start = next((e.t for e in events if e.type == "clock_started"), g.run.started_t)
    end = next((e.t for e in events if e.type == "finalize_started"), events[-1].t)
    rel = lambda t: mmss(t - start)                                     # noqa: E731
    w = g.run.workers[0] if g.run.workers else "w1"
    extra = re.compile(a.test_re) if a.test_re else None
    is_test = lambda cmd: bool(TEST_CMD.search(cmd) or (extra and extra.search(cmd)))   # noqa: E731
    calls = [c for c in tool_calls(bdir) if c["t"] and start <= c["t"] <= end]

    # 事件：快照（时间、树、相对链头的改动）、合并点、每棵树的命运
    snaps = [(e.t, e) for e in events if e.type == "snapshot_taken" and e.get("worker") == w]
    merges = [(e.t, e.get("tree")) for e in events if e.type == "merged"]
    fate: dict[str, str] = {}
    req_tree = {}
    for e in events:
        if e.type == "merge_requested":
            req_tree[e.get("attempt")] = e.get("tree")
            fate.setdefault(e.get("tree"), "请求中")
        elif e.type == "merged" and e.get("attempt"):
            fate[e.get("tree")] = "合并"
        elif e.type == "merge_rejected" and e.get("attempt") in req_tree:
            fate[req_tree[e.get("attempt")]] = {"regression": "回归门拦下", "review": "复核不批准"}.get(
                e.get("reason"), f"拒绝（{e.get('reason')}）")
        elif e.type == "merge_superseded" and e.get("attempt") in req_tree:
            fate.setdefault(req_tree[e.get("attempt")], "被取代")

    def head_at(t: float) -> Optional[str]:
        h = None
        for mt, tree in merges:
            if mt > t:
                break
            h = tree
        return h

    def snap_at(t: float):
        s = None
        for st, e in snaps:
            if st > t + 1e-3:
                break
            s = e
        return s

    def unmerged(t: float) -> tuple[int, int, Optional[str]]:
        s = snap_at(t)
        if s is None:
            return 0, 0, None
        if s.get("tree") == head_at(t):
            return 0, 0, s.get("tree")
        files = s.get("files") or []
        return len(files), sum(int(f[1]) + int(f[2]) for f in files), s.get("tree")

    # ---- 找稳定点
    points = []                                          # (时间, 类型, 说明)
    wrote_since = False
    last_write: Optional[float] = None
    for i, c in enumerate(calls):
        ok_write = c["name"] in ("edit_file", "write_file") and not c["error"]
        if last_write is not None and not ok_write and c["t"] - last_write >= a.quiet_min * 60 and wrote_since:
            points.append((last_write + a.quiet_min * 60, "编辑停顿", f"{a.quiet_min:g} 分钟没改文件"))
            wrote_since = False                          # 同一阵改动只算一次停顿
        if ok_write:
            last_write, wrote_since = c["t"], True
        if c["name"] == "bash" and is_test(c["cmd"]):
            if c["rc"] == 0 and (wrote_since or not points or points[-1][1] != "自测通过"):
                points.append((c["t"], "自测通过", c["cmd"][:60]))
                wrote_since = False
            elif c["rc"] not in (0, None):
                points.append((c["t"], "自测失败", c["cmd"][:60]))
    points.sort()

    n_write = sum(1 for c in calls if c["name"] in ("edit_file", "write_file") and not c["error"])
    n_test = sum(1 for c in calls if c["name"] == "bash" and is_test(c["cmd"]))
    kinds = {k: sum(1 for p in points if p[1] == k) for k in ("自测通过", "编辑停顿", "自测失败")}
    dur = max(1.0, (end - start) / 60)
    out = [f"# 稳定点：{bdir}", "",
           f"- 计时 {mmss(end - start)}；成功改文件 {n_write} 次，跑测试命令 {n_test} 次"
           + ("（含 --test-re）" if extra else ""),
           f"- 稳定点：自测通过 {kinds['自测通过']} 个、编辑停顿 {kinds['编辑停顿']} 个"
           f"（合计每 10 分钟约 {(kinds['自测通过'] + kinds['编辑停顿']) / dur * 10:.1f} 个）；另有自测失败 {kinds['自测失败']} 次", ""]

    rows = []
    for t, kind, note in points:
        nf, nl, tree = unmerged(t)
        rows.append([rel(t), kind, note.replace("|", "/"), nf, nl, fate.get(tree, "从没请求过") if nf else "（就是链头）"])
    out += table(["时间", "类型", "命令 / 说明", "未合并文件", "未合并行", "这棵树后来"], rows) if rows else ["（没有）"]

    # ---- 按候选规则回放
    stable = [(t, k) for t, k, _ in points if k in ("自测通过", "编辑停顿")]
    fired, last_req, last_merge = [], start, start
    real_merges = [t for t, _ in merges if t > start]
    minute = start
    while minute <= end + 30:
        for mt in real_merges:
            if mt <= minute:
                last_merge = max(last_merge, mt)
        if minute - last_req >= a.min_gap * 60 or last_req == start:
            # 上次请求之后最新的稳定点：在它那一刻的快照上发
            ready = next((p for p in reversed(stable) if last_req < p[0] <= minute), None)
            why = tree = None
            if ready is not None:
                nf, nl, tree = unmerged(ready[0])
                if nf and (nl >= a.lines or nf >= a.files):
                    why = f"{ready[1]}（{rel(ready[0])}），未合并 {nf} 个文件 / {nl} 行"
            if why is None:
                nf, nl, tree = unmerged(minute)
                if nf and minute - max(last_merge, last_req) >= a.cap * 60:
                    why = f"距上次合并或请求超过 {a.cap:g} 分钟（未合并 {nf} 个文件 / {nl} 行）"
            if why:
                fired.append((minute, why, fate.get(tree, "这棵树没被请求过：结局未知")))
                last_req = minute
        minute += 30
    out += ["", f"## 按候选规则回放（未合并 ≥ {a.lines} 行或 ≥ {a.files} 个文件且在稳定点；间隔 ≥ {a.min_gap:g} 分钟；"
                f"上限 {a.cap:g} 分钟）", ""]
    real_bg = [e for e in events if e.type == "merge_requested" and e.get("lane") == "bg"]
    out.append(f"- 会发 {len(fired)} 次后台请求（这次实际发了 {len(real_bg)} 次）")
    out += [""] + (table(["时间", "为什么发", "这棵树当时的结局"], [[rel(t), why, f] for t, why, f in fired])
                   if fired else ["（一次也不会发）"])
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python stable_points.py")
    ap.add_argument("targets", nargs="+", help="run_id、trial 目录或 Belay 的 run_dir")
    ap.add_argument("--test-re", default="", help="额外算作自测的命令（正则），例如 'simulate (run|score)'")
    ap.add_argument("--quiet-min", type=float, default=2, help="编辑停顿：多少分钟没改文件")
    ap.add_argument("--lines", type=int, default=50, help="回放：未合并行数阈值")
    ap.add_argument("--files", type=int, default=5, help="回放：未合并文件数阈值")
    ap.add_argument("--min-gap", type=float, default=5, help="回放：两次后台请求之间至少几分钟")
    ap.add_argument("--cap", type=float, default=20, help="回放：有未合并改动时最长多久必须发一次")
    a = ap.parse_args(argv)
    for target in a.targets:
        for _trial, bdir in find_targets(target):
            print("\n".join(analyse(bdir, a)) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
