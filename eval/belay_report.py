"""Belay 组件报告：一道题跑完后，逐个组件看它发挥得怎么样。

  python -m eval.belay_report v7-smoke-pydantic-5          # 一个 run_id 下的所有 Belay trial
  python -m eval.belay_report <trial 目录>                  # <results>/<run>/<benchmark>/<id>/<k>
  python -m eval.belay_report <...>/agent/belay            # 直接给 Belay 的 run_dir（没有评分信息）

输出 Markdown 到屏幕，同时写到 trial 目录的 belay_report.md（run_dir 模式写到 run_dir 里）；除此之外不改任何文件。

各节：
  评分      F2P / P2P 的失败项逐个对照 Belay 的视角：基线里是什么、在不在回归门里、交付的那棵树上测过没有、
            worker 的最终工作区里是否在（测试改动不交付）
  时间线    预算、首次改动、每个合并点、收尾、交付
  规划器    需求条数、最终状态（证据等级）、没完成的需求
  worker    会话、轮数、上下文峰值、压缩 / 交接、快照、todo
  合并链    合并请求按触发与结局统计、被拒原因、合并点列表（复核者标签）
  回归门    守护集合、各类验证作业的次数与耗时、拦下的回归、定位与诊断
  复核者    每次复核的耗时、跑了几条命令、结论、规则校验结果、失败与重试、token
  停滞      停滞、回滚、恢复
  交付      交付补丁与 worker 最终工作区的文件差别
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

from belay.core.events import Event
from belay.core.model import Graph
from belay.core.reduce import replay
from belay.core.render import ledger
from belay.core.verify import PT_FAIL, PT_PASS, active_guard, check_unit, guard_set, point_status
from eval.config import DEFAULT_RUNS, load_yaml, resolve_path


# ---------------------------------------------------------------- 定位文件
def _root(key: str) -> Path:
    return resolve_path(DEFAULT_RUNS.parent, load_yaml(DEFAULT_RUNS)[key])


def find_targets(target: str) -> list[tuple[Optional[Path], Path]]:
    """→ [(trial 目录或 None, belay run_dir)]"""
    p = Path(target)
    if p.is_dir() and (p / "events.jsonl").exists():
        return [(None, p)]
    base = p if p.is_dir() else _root("results_root") / target
    out = []
    for ev in sorted(base.rglob("agent/belay/events.jsonl")):
        bdir = ev.parent
        parts = bdir.parts
        trial = Path(*parts[:parts.index("pier")]) if "pier" in parts else None
        out.append((trial, bdir))
    if not out:
        sys.exit(f"在 {base} 下没有找到 Belay 的运行（agent/belay/events.jsonl）")
    return out


def load_graph(bdir: Path) -> tuple[Graph, list[Event]]:
    events = []
    with open(bdir / "events.jsonl", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(Event.from_dict(json.loads(line)))
    return replay(events), events


def _json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def agent_metadata(bdir: Path) -> dict:
    """pier/agent/<trial>/result.json 里 agent_result.metadata（复核者 token 等）。"""
    r = _json(bdir.parent.parent / "result.json")
    return ((r.get("agent_result") or {}).get("metadata")) or {}


# ---------------------------------------------------------------- 评分明细
def grade_details(trial: Path) -> Optional[dict]:
    """重新解析评分容器里的测试输出，得到失败的 F2P / P2P 测试名。"""
    logs = sorted(trial.glob("pier/grade/*/verifier/test_output.txt"))
    parts = trial.parts
    try:
        bench, tid = parts[-3], parts[-2]
    except IndexError:
        return None
    tdir = _root("task_dirs") / bench / tid
    tests = _json(tdir / "tests" / "tests.json")
    if not logs or not tests:
        return {"error": f"没找到评分输出或 tests.json（{trial}/pier/grade/*/verifier/test_output.txt，"
                         f"{tdir}/tests/tests.json）"}
    parser = "parse_log_pytest"
    sh = tdir / "tests" / "test.sh"
    if sh.exists():
        m = re.search(r"(parse_log_\w+)", sh.read_text(errors="replace"))
        parser = m.group(1) if m else parser
    from eval.convert.sweevo_to_harbor import GRADE_PY
    ns: dict = {"__name__": "grade"}
    exec(compile(GRADE_PY, "grade.py", "exec"), ns)               # noqa: S102 — 我们自己的评分脚本
    log = logs[-1].read_text(errors="replace")
    if ns["START"] not in log or ns["END"] not in log:
        return {"error": "评分输出里没有测试段（测试补丁没打上？）"}
    sm = ns["PARSERS"][parser](log.split(ns["START"])[1].split(ns["END"])[0])

    def bad(ts):
        return [t for t in ts if not (t in sm and sm[t] in ("PASSED", "XFAIL"))]
    return {"f2p_bad": bad(tests["FAIL_TO_PASS"]), "p2p_bad": bad(tests["PASS_TO_PASS"]),
            "status": sm, "n_f2p": len(tests["FAIL_TO_PASS"]), "n_p2p": len(tests["PASS_TO_PASS"])}


def _match(g: Graph, test: str) -> Optional[str]:
    """评分的测试名 → Belay 基线里的测试 id（完全相同，或去掉参数 / 路径前缀后唯一匹配）。"""
    if test in g.baseline:
        return test
    cands = [t for t in g.baseline if t.endswith(test) or test.endswith(t)]
    if len(cands) == 1:
        return cands[0]
    stem = test.split("[")[0]
    cands = [t for t in g.baseline if t.split("[")[0] == stem]
    return cands[0] if len(cands) == 1 else None


def explain_test(g: Graph, test: str, delivered_tree: Optional[str], final_tree: Optional[str]) -> str:
    tid = _match(g, test)
    if tid is None:
        unit = check_unit(test)
        known = any(check_unit(t) == unit for t in g.baseline)
        return ("基线里没有这个测试（所在文件" + ("在" if known else "不在") + "基线里：多半是评分时测试补丁新增或改写的）")
    cls = g.baseline.get(tid)
    guard = "在回归门里" if tid in active_guard(g) else ("被复核者豁免" if tid in g.waived else "不在回归门里")
    out = [f"基线 {cls}，{guard}"]
    for name, tree in (("交付树", delivered_tree), ("最终快照", final_tree)):
        if tree:
            st = point_status(g, tree, tid)
            out.append(f"{name}上 {st}" + ("" if st in (PT_PASS, PT_FAIL) else "（没测到）"))
    return "；".join(out)


# ---------------------------------------------------------------- 工具
ATT_NAMES = {"created": "合并", "rejected": "被拒", "superseded": "被取代", "pending": "进行中", "advancing": "推进中",
             "cancelled": "取消"}

def mmss(sec: Optional[float]) -> str:
    if sec is None:
        return "-"
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def table(head: list[str], rows: list[list]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in rows:
        out.append("| " + " | ".join(str(x).replace("|", "\\|").replace("\n", " ") for x in r) + " |")
    return out


def short(s, n=120) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def diff_files(p: Path) -> set[str]:
    try:
        return set(re.findall(r"^diff --git a/(\S+) b/", p.read_text(errors="replace"), re.M))
    except OSError:
        return set()


def transcript_turns(p: Optional[str], bdir: Path, vid: str) -> Optional[int]:
    path = Path(p) if p else bdir / "reviews" / f"{vid}.jsonl"
    if not path.exists():
        path = bdir / "reviews" / f"{vid}.jsonl"
    try:
        n = 0
        for line in path.read_text(errors="replace").splitlines():
            if '"type": "assistant"' in line or '"type":"assistant"' in line:
                n += 1
        return n
    except OSError:
        return None


# ---------------------------------------------------------------- 报告
def report(trial: Optional[Path], bdir: Path) -> str:
    g, events = load_graph(bdir)
    L = ledger(g)
    run = g.run
    t0 = run.started_t if run else (events[0].t if events else 0)
    rel = (lambda t: mmss(t - t0) if t else "-")
    out = [f"# Belay 组件报告：{trial or bdir}", ""]
    if run is None:
        return "\n".join(out + ["（事件日志里没有 run_started）"])
    if run.version != 8:
        out.append(f"> 注意：这是 v{run.version} 的日志，报告按 v8 的组件来写")

    by_type: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        by_type[e.type].append(e)
    delivered = g.checkpoints.get(run.delivered) if run.delivered is not None else None
    snaps = sorted(g.snapshots.values(), key=lambda s: s.n)
    final_snap = snaps[-1] if snaps else None

    # ---- 评分
    out += ["## 评分", ""]
    gd = grade_details(trial) if trial else None
    grade = _json(trial / "grade.json") if trial else {}
    rw = grade.get("rewards") or {}
    if grade:
        out.append(f"- fix_rate {grade.get('fix_rate')}，F2P {rw.get('f2p_success')}/"
                   f"{(rw.get('f2p_success') or 0) + (rw.get('f2p_failure') or 0)}，P2P 失败 {rw.get('p2p_failure')}"
                   "（P2P 只要有一个失败，fix_rate 就是 0）")
    if trial is None:
        out.append("- （直接给的 run_dir：没有评分信息）")
    if gd and gd.get("error"):
        out.append(f"- 失败明细：{gd['error']}")
    elif gd:
        for kind, key in (("P2P（原本通过、现在失败）", "p2p_bad"), ("F2P（应该修好、没修好）", "f2p_bad")):
            bad = gd[key]
            if not bad:
                continue
            out += ["", f"失败的 {kind}：{len(bad)} 个" + ("，只列前 15 个" if len(bad) > 15 else ""), ""]
            rows = [[t, gd["status"].get(t, "没跑出结果"),
                     explain_test(g, t, delivered.tree if delivered else None,
                                  final_snap.raw_tree if final_snap else None)] for t in bad[:15]]
            out += table(["测试", "评分结果", "Belay 的视角"], rows)
    out.append("")

    # ---- 时间线
    budget = run.budget_sec
    first_edit = next((s for s in snaps if s.files), None)
    merges = [c for c in sorted(g.checkpoints.values(), key=lambda c: c.id) if c.id > 0]
    fin = by_type.get("finalize_started", [])
    dl = by_type.get("delivered", [])
    out += ["## 时间线", "",
            f"- 预算 {mmss(budget)}；基线全量约 {int(g.baseline_sec)}s；"
            f"{'回归门可用' if g.baseline else '没有回归门（无测试）'}"
            + ("；导入隔离无效（降级）" if g.degraded else ""),
            f"- 第一次改动 {rel(first_edit.t) if first_edit else '-'}；第一个合并点 {rel(merges[0].created_t) if merges else '-'}；"
            f"合并点 {len(merges)} 个（链头 #{g.head}）",
            f"- 收尾 {rel(fin[0].t) if fin else '-'}（{run.finalize_reason or '-'}）；交付 #{run.delivered} "
            f"{rel(dl[-1].t) if dl else '-'}；状态 {L['status']}"
            + (f"；未 DONE 的原因：{'; '.join(L['status_reasons'])}" if L["status_reasons"] else ""),
            f"- 会话 {len(g.sessions)} 个；恢复 {run.recoveries} 次，停机 {int(run.downtime_sec)}s", ""]
    if run.improving or g.improvements:                       # after_accept=improve
        st = by_type.get("improve_started", [])
        after = [c for c in merges if run.improve_seq and c.created_seq > run.improve_seq]
        before = [c for c in merges if not run.improve_seq or c.created_seq <= run.improve_seq]
        s0 = next((c.score for c in reversed(before) if c.score is not None), None)
        s1 = delivered.score if delivered is not None else None
        mode = {"verify": "VERIFY", "improve": "IMPROVE"}.get(run.polish_mode, run.polish_mode or "-")
        start_cp = L["improve"].get("start_checkpoint")
        out.insert(-1, f"- 改进阶段（{mode}，开始时链头 #{start_cp}）：开始 {rel(st[0].t) if st else '-'}；"
                       + (f"结束：{short(run.improve_closed, 100)}；" if run.improve_closed else "")
                       + f"改进项 {len(g.improvements)} 条（"
                       + "，".join(f"{k} {v}" for k, v in Counter(i.status for i in g.improvements.values()).items())
                       + f"）；开始后合并点 {len(after)} 个"
                       + (f"；分数 {s0:g} → {s1:g}" if s0 is not None and s1 is not None else ""))

    # ---- 规划器
    acts = [r for r in g.requirements.values() if r.kind == "actionable"]
    out += ["## 规划器与需求", "",
            f"- 规划 {len(g.plans)} 轮；需求 {len(g.requirements)} 条，其中可执行 {len(acts)} 条、背景 "
            f"{len(g.requirements) - len(acts)} 条；带已有测试作证据的 {sum(1 for r in acts if r.checks)} 条",
            "- 最终：" + "，".join(f"{k} {v}" for k, v in L["categories"].items() if v)
            + f"（计为完成 {L['done_counted']}）"]
    by_src = Counter((r.by or "-") for r in acts if r.status == "done")
    if by_src:
        out.append("- 完成判定来源：" + "，".join(f"{k} {v}" for k, v in by_src.items()))
    left = [r for r in acts if r.status != "done"]
    if left:
        out += ["", "没完成的需求：", ""]
        out += table(["需求", "状态", "摘要", "缺什么 / 受阻原因", "被判未完成次数"],
                     [[r.id, r.status, short(r.summary or r.quote, 60),
                       short("; ".join(r.missing) or r.blocked_reason or "", 120), r.misses] for r in left[:30]])
    out.append("")

    # ---- worker
    out += ["## worker 会话", ""]
    rows = []
    for s in sorted(g.sessions.values(), key=lambda s: s.n):
        lv = Counter(c[0] if isinstance(c, (tuple, list)) else c for c in s.compactions)
        rows.append([s.id, s.reason, s.end_reason or "（进行中）", s.turns, f"{s.peak_context:,}",
                     ", ".join(f"L{k}×{v}" for k, v in sorted(lv.items())) or "-",
                     mmss((s.ended_t or (dl[-1].t if dl else s.started_t)) - s.started_t), "是" if s.progress else "否"])
    out += table(["会话", "开始原因", "结束原因", "轮数", "上下文峰值", "压缩", "时长", "有进展"], rows)
    sr = Counter(s.reason for s in snaps)
    todos = list(g.todos.values())
    out += ["", f"- 快照 {len(snaps)} 个（" + "，".join(f"{k} {v}" for k, v in sr.most_common()) + "）；"
            f"不可测（预检失败）{sum(1 for s in snaps if not s.testable)} 个",
            f"- todo {len(todos)} 条" + ("：" + "，".join(f"{k} {v}" for k, v in Counter(t.status for t in todos).items())
                                         if todos else ""),
            f"- worker 提交 {len(g.submits)} 次：" + "，".join(f"{k} {v}" for k, v in
                                                       Counter(s.status for s in g.submits.values()).items()), ""]

    # ---- 合并链
    atts = sorted(g.attempts.values(), key=lambda a: a.created_seq)
    out += ["## 合并链", ""]
    trig = defaultdict(Counter)
    for a in atts:
        trig[a.trigger][a.status] += 1
    statuses = sorted({s for c in trig.values() for s in c})
    out += table(["触发"] + [ATT_NAMES.get(s, s) for s in statuses] + ["合计"],
                 [[t] + [c.get(s, 0) for s in statuses] + [sum(c.values())] for t, c in trig.items()])
    rej = Counter(a.reason for a in atts if a.status == "rejected")
    if rej:
        out += ["", "- 被拒原因：" + "，".join(f"{k} {v}" for k, v in rej.most_common())]
    end_t = {e.get("attempt"): e.t for e in events if e.type in ("merged", "merge_rejected", "merge_superseded")}
    waits = [end_t[a.id] - a.created_t for a in atts if a.id in end_t and a.status in ("merged", "rejected")]
    if waits:
        out.append(f"- 合并请求从发出到有结局：中位 {mmss(sorted(waits)[len(waits) // 2])}，最长 {mmss(max(waits))}")
    out += [""] + table(["合并点", "时间", "触发", "文件数", "复核", "分数", "标签"],
                        [[f"#{c.id}" + (" (abandoned)" if c.abandoned else "") + (" ← 交付" if c.id == run.delivered
                                                                                    else ""),
                          rel(c.created_t), c.trigger, len(c.files), c.review or "只凭回归门",
                          "-" if c.score is None else c.score, short(c.label, 90)] for c in merges])
    out.append("")

    # ---- 回归门与验证
    jobs = list(g.jobs.values())
    out += ["## 回归门与验证作业", ""]
    if g.baseline:
        bc = Counter(g.baseline.values())
        out += [f"- 基线 {len(g.baseline)} 个测试：" + "，".join(f"{k} {v}" for k, v in bc.items())
                + f"；回归门守护 {len(guard_set(g.baseline))} 个，豁免 {len(g.waived)} 个", ""]
    jrows = []
    for purpose in sorted({j.purpose for j in jobs}):
        js = [j for j in jobs if j.purpose == purpose]
        st = Counter(j.state for j in js)
        jrows.append([purpose, len(js), ", ".join(f"{k} {v}" for k, v in st.items()), int(sum(j.sec for j in js)),
                      sum(j.preemptions for j in js)])
    out += table(["用途", "次数", "结局", "总耗时 s", "被抢占"], jrows)
    caught = [a for a in atts if a.reason == "regression"]
    if caught:
        tests = Counter(t for a in caught for t in a.regressions)
        out += ["", f"- 回归门拦下 {len(caught)} 次合并；最常见的回归：" +
                "，".join(f"{short(t, 70)}×{n}" for t, n in tests.most_common(5))]
    out.append(f"- 定位 {len(g.locates)} 次，诊断 {len(g.diagnoses)} 次，持续回归 {len(g.persistent)} 个")
    out.append("")

    # ---- 复核者
    revs = sorted(g.reviews.values(), key=lambda v: v.seq)
    done_t = {e.get("review"): e.t for e in events if e.type in ("merge_reviewed", "review_cancelled")}
    out += ["## 复核者", ""]
    rows = []
    for v in revs:
        dec = v.decision or {}
        verdict = v.verdict or {}
        js = Counter(f"{j.get('status')}" + (f"/{j['level']}" if j.get("level") else "")
                     for j in (dec.get("judgements") or []))
        conclusion = {True: "建议合并", False: "不合并"}.get(verdict.get("merge"), "只判定")
        ruled = {True: "通过", False: "驳回", None: "-"}.get(dec.get("merge"), "-")
        rows.append([v.id + (f"（重试 {v.retry_of}）" if v.retry_of else ""), v.trigger, v.attempt or f"#{v.checkpoint}",
                     v.status, mmss(done_t[v.id] - v.t) if v.id in done_t else "-",
                     transcript_turns(v.transcript, bdir, v.id), len(v.runs),
                     sum(1 for r in v.runs if r.get("rc") not in (0, None)),
                     conclusion if v.status != "failed" else short(v.error, 50), ruled,
                     ", ".join(f"{k} {n}" for k, n in js.items()) or "-",
                     short("; ".join(dec.get("reasons") or []) or verdict.get("reason") or "", 100)])
    out += [""] if out[-1] else []
    out += table(["复核", "触发", "对象", "状态", "耗时", "轮数", "命令", "非零退出", "复核者结论", "规则校验",
                  "需求判定", "原因"], rows)
    out.append("")
    meta = agent_metadata(bdir)
    rt = meta.get("review_tokens")
    if rt:
        out += [f"- 复核者 token：输入 {rt.get('input', 0):,}（缓存 {rt.get('cache', 0):,}），输出 {rt.get('output', 0):,}"]
    rev_sec = sum(done_t[v.id] - v.t for v in revs if v.id in done_t)
    if dl:
        out.append(f"- 复核总耗时 {mmss(rev_sec)}，占运行时长 {rev_sec / max(1.0, dl[-1].t - t0):.0%}（与 worker 并行，"
                   "但提交时 worker 要等复核结论）")
    lv = Counter(r.level for r in acts if r.status == "done")
    if lv:
        out.append("- 完成需求的证据等级：" + "，".join(f"{k} {v}" for k, v in sorted(lv.items())))
    if g.waived:
        out.append(f"- 豁免：{', '.join(list(g.waived)[:10])}")
    out.append("")

    # ---- 停滞、回滚
    rb = by_type.get("rollback", [])
    out += ["## 停滞与回滚", ""]
    if g.stalls:
        out += table(["时间", "类型", "动作", "说明"], [[rel(s.t), s.kind, s.action, short(s.detail, 100)]
                                                      for s in g.stalls])
    else:
        out.append("- 没有停滞")
    out += [f"- 回滚 {len(rb)} 次", ""]

    # ---- 交付
    out += ["## 交付", ""]
    dfiles, wfiles = diff_files(bdir / "deliverable.diff"), diff_files(bdir / "worktree.diff")
    if dfiles or wfiles:
        out.append(f"- 交付补丁 {len(dfiles)} 个文件；worker 最终工作区改了 {len(wfiles)} 个文件")
        only_wt = sorted(wfiles - dfiles)
        if only_wt:
            out.append(f"- 工作区改了但没交付的（测试改动不交付，或停在链头之后没合并）：{', '.join(only_wt[:15])}")
    if delivered is not None and g.head is not None and delivered.id != g.head:
        out.append(f"- 交付的不是链头：交付 #{delivered.id}，链头 #{g.head}")
    if final_snap is not None and g.head_cp is not None and final_snap.tree != g.head_cp.tree:
        out.append(f"- worker 最后一个快照 s{final_snap.n}（{rel(final_snap.t)}）没进合并链："
                   "收尾时它的合并请求没通过或来不及")
    out.append("")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eval.belay_report")
    ap.add_argument("target", help="run_id、trial 目录或 Belay 的 run_dir")
    a = ap.parse_args(argv)
    for trial, bdir in find_targets(a.target):
        md = report(trial, bdir)
        print(md)
        dest = (trial or bdir) / "belay_report.md"
        try:
            dest.write_text(md, encoding="utf-8")
            print(f"（已写到 {dest}）\n")
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
