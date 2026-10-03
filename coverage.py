"""交付覆盖分析：只读一次（或多次）Belay 运行的事件日志，回答“中途被杀会交付什么、后台存档为什么没跟上”。

不改任何代码与文件。放在 belay/ 目录下运行（需要项目的 venv）：
  python coverage.py v9-conan                       # 一个 run_id 下的所有 Belay trial
  python coverage.py v9-conan v9-spot-improve-2     # 几个一起看
  python coverage.py <trial 目录或 agent/belay 目录>
  python coverage.py v9-conan --every 5             # 被杀时间点的间隔（分钟），默认 10

各节：
  1. 合并点与空窗    每个合并点的时间；从第一次改动起，相邻合并点之间最长隔了多久；链头落后于工作区的总时长
  2. 中途被杀        每隔 N 分钟：此刻被杀会交付哪个合并点、计为完成的需求几条、链头分数、未合并的文件数
  3. todo 勾选       勾了几条、分几批、每批几条、什么时候；每批之后有没有发起 todo 触发的后台合并
  4. 后台合并请求    每个后台请求的触发、结局（合并 / 回归门拦下 / 复核不批准 / 被取代）、拦下它的测试
  5. 假设分析        （只是估计，不重跑）
     a. 后台也能豁免：被回归门拦下的后台请求里，回归全部是“之后被复核者豁免的测试”或“上一次后台请求也挂的测试”的有几次
     b. 按未合并改动量提前触发：未合并文件 ≥ --lag-files 且距上次后台复核 ≥ --lag-min 分钟、却还没到 15 分钟兜底的时间点
时间都从计时开始（clock_started；没有时从 run_started）算。
"""
from __future__ import annotations

import argparse
from collections import Counter
from typing import Optional

from belay.core.queries import done_count, last_score, latest_snapshot
from belay.core.reduce import replay
from belay.core.verify import regression_ids
from eval.belay_report import find_targets, load_graph


def mmss(sec: Optional[float]) -> str:
    if sec is None:
        return "-"
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def table(head: list[str], rows: list[list]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(str(x) for x in r) + " |" for r in rows]
    return out


def analyse(bdir, every_min: float, lag_files: int, lag_min: float, auto_min: float) -> list[str]:
    g, events = load_graph(bdir)
    if g.run is None or not events:
        return [f"# {bdir}", "", "（事件日志是空的）"]
    start = next((e.t for e in events if e.type == "clock_started"), g.run.started_t)
    end = next((e.t for e in events if e.type == "finalize_started"), events[-1].t)
    rel = lambda t: mmss(t - start)                                     # noqa: E731
    w = g.run.workers[0] if g.run.workers else "w1"
    out = [f"# 交付覆盖：{bdir}", "",
           f"- 计时 {mmss(end - start)}（到收尾开始；收尾原因 {g.run.finalize_reason or '-'}）；"
           f"预算 {mmss(g.run.budget_sec)}", ""]

    # ---- 1. 合并点与空窗
    merges = [(e.t, int(e.get("checkpoint"))) for e in events if e.type == "merged" and int(e.get("checkpoint")) > 0]
    first_edit = next((e.t for e in events if e.type == "snapshot_taken" and e.get("files")), None)
    out += ["## 1. 合并点与空窗", ""]
    rows = []
    for t, cid in merges:
        cp = g.checkpoints.get(cid)
        if cp is None:
            continue
        rows.append([f"#{cid}", rel(t), cp.trigger, len(cp.files), "-" if cp.score is None else f"{cp.score:g}",
                     "被回退" if cp.abandoned else ""])
    out += table(["合并点", "时间", "触发", "文件", "分数", ""], rows) if rows else ["（没有合并点）"]
    if first_edit is not None:
        pts = [first_edit] + [t for t, _ in merges if t >= first_edit] + [end]
        gaps = [(b - a, a, b) for a, b in zip(pts, pts[1:])]
        worst = max(gaps)
        out += ["", f"- 第一次改动 {rel(first_edit)}；之后最长的空窗 **{mmss(worst[0])}**"
                f"（{rel(worst[1])} → {rel(worst[2])}）；空窗中位数 {mmss(sorted(x[0] for x in gaps)[len(gaps) // 2])}"]
    # 链头落后于工作区的时长（最新快照的候选树 ≠ 链头的树）
    lag_total, lag_long, lag_from, head_tree, snap_tree = 0.0, (0.0, None), None, None, None
    for e in events:
        if e.type == "merged":
            head_tree = e.get("tree")
        elif e.type == "snapshot_taken" and e.get("worker") == w:
            snap_tree = e.get("tree")
        if e.t > end:
            break
        lagging = head_tree is not None and snap_tree is not None and snap_tree != head_tree
        if lagging and lag_from is None:
            lag_from = e.t
        elif not lagging and lag_from is not None:
            lag_total += e.t - lag_from
            lag_long = max(lag_long, (e.t - lag_from, lag_from))
            lag_from = None
    if lag_from is not None:
        lag_total += end - lag_from
        lag_long = max(lag_long, (end - lag_from, lag_from))
    out.append(f"- 链头落后于工作区：共 {mmss(lag_total)}，占计时的 {lag_total / max(1.0, end - start):.0%}；"
               f"最长一段 {mmss(lag_long[0])}" + (f"（从 {rel(lag_long[1])} 起）" if lag_long[1] else ""))

    # ---- 2. 中途被杀
    out += ["", f"## 2. 中途被杀会交付什么（每 {every_min:g} 分钟）", ""]
    rows, k = [], 0
    ts = start + every_min * 60
    while ts < end + every_min * 60:
        ts = min(ts, end)
        while k < len(events) and events[k].t <= ts:
            k += 1
        gk = replay(events[:k])
        head = gk.head_cp
        snap = latest_snapshot(gk, w)
        unmerged = 0 if snap is None or head is None or snap.tree == head.tree else (
            len(snap.files) if snap.base == head.id else "?")
        score = last_score(gk)[0] if gk.checkpoints else None
        rows.append([rel(ts), f"#{head.id}" if head else "-", done_count(gk),
                     "-" if score is None else f"{score:g}", unmerged])
        if ts >= end:
            break
        ts += every_min * 60
    n_act = sum(1 for r in g.requirements.values() if r.kind == "actionable")
    out += table(["被杀时刻", "交付的合并点", f"计为完成的需求（共 {n_act}）", "链头分数", "未合并的文件"], rows)

    # ---- 3. todo 勾选
    out += ["", "## 3. todo 勾选", ""]
    ticks = [e for e in events if e.type == "todo_completed"]
    batches: list[list] = []
    for e in ticks:
        if batches and abs(e.t - batches[-1][0].t) < 1e-6:
            batches[-1].append(e)
        else:
            batches.append([e])
    todo_reqs = [e for e in events if e.type == "merge_requested" and e.get("trigger") == "todo"]
    if not ticks:
        out.append("- 没有勾选过 todo")
    else:
        sizes = Counter(len(b) for b in batches)
        out.append(f"- 勾了 {len(ticks)} 条，分 {len(batches)} 批；每批条数分布："
                   + "，".join(f"{n} 条×{c}" for n, c in sorted(sizes.items())) + f"；todo 触发的后台请求 {len(todo_reqs)} 个")
        rows = []
        for b in batches:
            t = b[0].t
            nxt = next((e for e in todo_reqs if e.t >= t), None)
            rows.append([rel(t), len(b), "; ".join((g.todos[e.get("todo")].title if e.get("todo") in g.todos
                                                    else str(e.get("todo")))[:40] for e in b)[:120],
                         f"{rel(nxt.t)}（{mmss(nxt.t - t)} 后）" if nxt and nxt.t - t < 600 else "没有（10 分钟内）"])
        out += [""] + table(["时间", "条数", "条目", "之后的 todo 后台请求"], rows)

    # ---- 4. 后台合并请求
    out += ["", "## 4. 后台合并请求", ""]
    outcome: dict[str, tuple] = {}
    for e in events:
        if e.type in ("merged",) and e.get("attempt"):
            outcome[e.get("attempt")] = ("合并", e.t, ())
        elif e.type == "merge_rejected":
            outcome[e.get("attempt")] = (f"拒绝：{e.get('reason')}", e.t, tuple(e.get("regressions") or ()))
        elif e.type == "merge_superseded":
            outcome[e.get("attempt")] = ("被取代", e.t, ())
    bg = [e for e in events if e.type == "merge_requested" and e.get("lane") == "bg"]
    rows = []
    for e in bg:
        res, t2, regs = outcome.get(e.get("attempt"), ("未结束", None, ()))
        ids = regression_ids(regs)
        rows.append([e.get("attempt"), rel(e.t), e.get("trigger"), f"s{e.get('snapshot')}", res,
                     mmss(t2 - e.t) if t2 else "-",
                     ", ".join(t.split("::")[-1] for t in ids[:3]) + (f" 等 {len(ids)} 个" if len(ids) > 3 else "")])
    out += table(["请求", "时间", "触发", "快照", "结局", "用时", "拦下它的测试"], rows) if rows else ["（没有后台请求）"]

    # ---- 5. 假设分析
    out += ["", "## 5. 假设分析（估计，不重跑）", ""]
    waived = set(g.waived)
    prev_regs: set = set()
    eligible = []
    for e in bg:
        res, _t2, regs = outcome.get(e.get("attempt"), ("", None, ()))
        ids = {t.split(" [", 1)[0] for t in regression_ids(regs)}
        if res == "拒绝：regression" and ids:
            why = []
            if ids <= waived:
                why.append("这些测试后来被复核者豁免了")
            if ids <= prev_regs:
                why.append("上一次被拦的后台请求也挂这些")
            if why:
                eligible.append((e, why))
            prev_regs = ids
    out.append(f"- a. 后台也能豁免：{len(eligible)} 次被回归门拦下的后台请求会改为请复核者裁决"
               + ("：" + "；".join(f"{x.get('attempt')}@{rel(x.t)}（{'、'.join(why)}）" for x, why in eligible)
                  if eligible else "") + "。会不会合并，取决于复核者能不能引用到任务原文。")
    # b. 按未合并改动量提前触发
    reviews_t = sorted(e.t for e in events if e.type == "review_started" and e.get("trigger") in
                       ("auto", "todo", "handoff", "session_end"))
    extra, k, minute = [], 0, start
    while minute <= end:
        while k < len(events) and events[k].t <= minute:
            k += 1
        gk = replay(events[:k])
        snap, head = latest_snapshot(gk, w), gk.head_cp
        files = len(snap.files) if snap is not None and head is not None and snap.tree != head.tree \
            and snap.base == head.id else 0
        last = max([t for t in reviews_t if t <= minute], default=start)
        busy = any(a.status in ("pending", "advancing") and a.lane == "bg" for a in gk.attempts.values())
        if files >= lag_files and lag_min * 60 <= minute - last < auto_min * 60 and not busy and \
                (not extra or minute - extra[-1] >= lag_min * 60):
            extra.append(minute)
        minute += 60
    out.append(f"- b. 未合并文件 ≥ {lag_files} 时把兜底间隔从 {auto_min:g} 分钟缩到 {lag_min:g} 分钟：会多出约 "
               f"{len(extra)} 次后台请求" + ("（" + "、".join(rel(t) for t in extra[:12]) + "）" if extra else "")
               + "。每次是否合并取决于回归门与复核者。")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python coverage.py")
    ap.add_argument("targets", nargs="+", help="run_id、trial 目录或 Belay 的 run_dir")
    ap.add_argument("--every", type=float, default=10, help="“中途被杀”的时间点间隔（分钟）")
    ap.add_argument("--lag-files", type=int, default=5, help="假设 b：未合并文件数阈值")
    ap.add_argument("--lag-min", type=float, default=5, help="假设 b：缩短后的兜底间隔（分钟）")
    ap.add_argument("--auto-min", type=float, default=15, help="现在的兜底间隔（分钟，merge_min_interval_sec）")
    a = ap.parse_args(argv)
    for target in a.targets:
        for _trial, bdir in find_targets(target):
            print("\n".join(analyse(bdir, a.every, a.lag_files, a.lag_min, a.auto_min)) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
