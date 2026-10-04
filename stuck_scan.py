"""打转检测（只读）：在已经跑完的 Belay 运行里找“模型卡住、在原地打转”的片段，并看它后来有没有自己走出来。

不改任何代码与文件，不调用模型。放在 belay/ 目录下运行（需要项目的 venv）：
  python stuck_scan.py v10-conan v10-spot-improve        # 一个或几个 run_id 下的所有 Belay trial
  python stuck_scan.py <trial 目录或 agent/belay 目录>
  python stuck_scan.py v10-conan --out stuck.md --json stuck.json
  python stuck_scan.py v10-spot --test-re "simulate (run|score)"   # 把任务自带的验证命令也算作测试

数据来源：事件日志 events.jsonl（运行时自己的“进展”定义、会话、合并、复核、停滞信号）与每个会话的轨迹
sessions/S<n>.jsonl（模型的每个工具调用与输出）。

一、会话内的模式（参照 OpenHands 的 StuckDetector，逐个工具调用按顺序看，同一会话内）：
  repeat     同一动作得到同一观察，连续 ≥ --repeat 次（默认 4）            动作 = 工具名 + 参数；观察 = 输出（去掉耗时、地址）
  error      同一动作连续 ≥ --error-repeat 次都出错（默认 3）               出错 = 工具报错，或 bash 退出码非 0
  monologue  模型连续 ≥ --monologue 次只说话、不调用工具（默认 3）
  alternate  两组“动作 + 观察”来回交替（A B A B A B），≥ --alternate 步（默认 6）
Belay 特有、比逐字相同更宽的两种：
  samefail   改过文件之后再跑测试 / 程序，仍是同一个失败签名，连续 ≥ --same-fail 次（默认 3）
             签名 = 失败的测试名集合（pytest / go / cargo / jest 的输出格式），认不出时取输出尾部
  revisit    工作区回到了这个会话里出现过的同一棵树、且中间有别的树（改来改去），同一棵树出现 ≥ --revisits+1 次（默认 2）

二、无进展窗口：进展用运行时自己的定义（reduce 里的 _progress：需求完成 / 受阻被认可、证据检查第一次通过、
   改进项完成、分数提高），逐个事件重放得到。每个会话里不短于 --min-window 分钟（默认 10）的无进展窗口，
   以及它怎么结束的（有了进展 = 自己走出来；会话结束；收尾）。最后按时长分档：卡住 ≥ t 分钟的窗口里，后来自己
   走出来的有几个——这就是定阈值要看的“卡住 t 分钟之后还能自己出来的概率”。

三、每次命中之后：同一会话里多久之后有了进展（自己走出来），还是一直到会话结束都没有。

四、运行时已有的信号：stall_detected（提醒 / 换人）；同一需求、同一改进项被复核者连续判为没做完的最长连续次数；
   最后一个合并点之后的请求都怎么被拒的（回归门 / 复核者 / 分数下降 …）。

时间都从计时开始（clock_started；没有时从 run_started）算，mm:ss。标出命中是在 POLISH（improve_started）之前还是之后。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

from belay.core.model import Graph
from belay.core.reduce import apply
from belay.runtime.shellcmd import is_run_command, is_test_command
from eval.belay_report import find_targets

EXIT = re.compile(r"\[exit code (-?\d+)")
TIMEOUT = re.compile(r"\[Command timed out after \d+s")
WRITES = ("edit_file", "write_file")
SESSION_FILE = re.compile(r"^S(\d+)\.jsonl$")
# 失败签名：各种测试框架的失败行
FAIL_PATTERNS = [re.compile(p, re.M) for p in (
    r"^(?:FAILED|ERROR) (\S+)",                       # pytest -rf / 汇总
    r"^\S+::\S+ (?:FAILED|ERROR)\b",                  # pytest -v
    r"^--- FAIL: (\S+)",                              # go test
    r"^test (\S+) \.\.\. FAILED",                     # cargo test
    r"^\s*[✕×] (.+?)(?: \(\d+ ?ms\))?$",              # jest
)]
NOISE = [(re.compile(p), r) for p, r in (
    (r"\b\d+(?:\.\d+)?\s*(?:s|ms|sec|seconds)\b", "<t>"),   # 耗时
    (r"0x[0-9a-fA-F]+", "<addr>"),                           # 地址
    (r"\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:\.\d+)?", "<ts>"),  # 时间戳
    (r"/tmp/\S+", "<tmp>"),
)]


def mmss(sec: Optional[float]) -> str:
    if sec is None:
        return "-"
    sec = int(round(sec))
    sign = "-" if sec < 0 else ""
    sec = abs(sec)
    return f"{sign}{sec // 60}:{sec % 60:02d}"


def minutes(sec: Optional[float]) -> str:
    return "-" if sec is None else f"{sec / 60:.1f}"


def table(head: list[str], rows: list[list]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    return out + ["| " + " | ".join(str(x).replace("|", "\\|").replace("\n", " ") for x in r) + " |" for r in rows]


def _norm(text: str) -> str:
    for pat, rep in NOISE:
        text = pat.sub(rep, text)
    return text


def _digest(text: str) -> str:
    return hashlib.sha1(_norm(text).encode("utf-8", "replace")).hexdigest()[:12]


def failure_signature(out: str) -> str:
    """失败的测试名集合；认不出时取输出尾部（去掉退出码那行与噪声）的摘要。"""
    names = set()
    for pat in FAIL_PATTERNS:
        for m in pat.finditer(out):
            names.add((m.group(1) if m.groups() else m.group(0)).strip()[:200])
    if names:
        s = ",".join(sorted(names))
        return f"tests:{len(names)}:{hashlib.sha1(s.encode()).hexdigest()[:10]}"
    lines = [x for x in out.strip().splitlines() if x.strip() and not EXIT.search(x)][-15:]
    return f"tail:{_digest(chr(10).join(lines))}"


# ======================================================================== 读数据

def load_events(bdir: Path) -> list:
    from belay.core.events import Event
    out = []
    with open(bdir / "events.jsonl", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(Event.from_dict(json.loads(line)))
    return out


def progress_times(events: list) -> tuple[list[float], str]:
    """运行时自己的进展定义：逐个事件重放，last_progress_seq 变化的时刻。重放不了（日志版本不同）时退回近似：
    需求从未完成变成完成 / 受阻、改进项完成、复核决定里分数提高。"""
    try:
        g, out, last = Graph(), [], None
        for e in events:
            g = apply(g, e)
            if g.last_progress_seq != last and g.last_progress_seq == e.seq:
                out.append(e.t)
            last = g.last_progress_seq
        return out, "runtime"
    except Exception as ex:                             # noqa: BLE001
        status: dict[str, str] = {}
        out = []
        for e in events:
            if e.type == "requirement_judged":
                prev = status.get(e.get("requirement"), "open")
                status[e.get("requirement")] = e.get("status")
                if prev == "open" and e.get("status") in ("done", "blocked"):
                    out.append(e.t)
            elif e.type == "improvement_judged" and e.get("status") == "done":
                out.append(e.t)
            elif e.type == "review_decided" and e.get("improved"):
                out.append(e.t)
        return sorted(out), f"approx ({type(ex).__name__}: {str(ex)[:120]})"


def tool_calls(path: Path) -> tuple[list[dict], list[float]]:
    """一个会话轨迹里的工具调用（按顺序）与“只说话不调用工具”的模型回复时刻。"""
    calls: list[dict] = []
    talk: list[float] = []
    pending: dict[str, dict] = {}
    seq = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        typ = r.get("type")
        if typ == "message":
            m = r.get("message") or {}
            if m.get("role") != "assistant" or not isinstance(m.get("content"), list):
                continue
            uses = [b for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_use"]
            for b in uses:
                pending[b.get("id")] = b.get("input") or {}
            if not uses:
                talk.append(r.get("t") or 0.0)
                calls.append({"t": r.get("t") or 0.0, "talk": True, "seq": seq})
                seq += 1
        elif typ == "tool_result":
            for x in r.get("results") or []:
                inp = pending.pop(x.get("id"), {})
                out = str(x.get("output") or "")
                name = x.get("name") or "?"
                m = EXIT.search(out[-400:])
                rc = int(m.group(1)) if m else (124 if TIMEOUT.search(out[-400:]) else None)
                err = bool(x.get("error")) or (rc not in (None, 0))
                cmd = str(inp.get("command") or "") if name == "bash" else ""
                calls.append({"t": r.get("t") or 0.0, "talk": False, "seq": seq, "name": name, "cmd": cmd,
                              "action": name + ":" + json.dumps(inp, sort_keys=True, ensure_ascii=False)[:4000],
                              "obs": _digest(out), "error": err, "rc": rc, "out": out,
                              "write": name in WRITES and not x.get("error")})
                seq += 1
    return calls, talk


# ======================================================================== 检测

def _hit(kind: str, t: float, detail: str, n: int) -> dict:
    return {"kind": kind, "t": t, "detail": detail, "n": n}


def detect_calls(calls: list[dict], a, is_check) -> list[dict]:
    """会话内的模式。每个模式在一段连续里只报一次（第一次达到阈值的时刻），n 是这一段最终的长度。"""
    hits: list[dict] = []
    acts = [c for c in calls if not c["talk"]]

    def runs(key, cond=lambda c: True):
        """连续相同 key（且满足 cond）的段：[(起点下标, 长度)]"""
        out, i = [], 0
        while i < len(acts):
            if not cond(acts[i]):
                i += 1
                continue
            j = i
            while j + 1 < len(acts) and cond(acts[j + 1]) and key(acts[j + 1]) == key(acts[i]):
                j += 1
            out.append((i, j - i + 1))
            i = j + 1
        return out

    for i, n in runs(lambda c: (c["action"], c["obs"])):
        if n >= a.repeat:
            c = acts[i + a.repeat - 1]
            what = c["cmd"][:80] or c["action"][len(c["name"]) + 1:][:80]
            hits.append(_hit("repeat", c["t"], f"{c['name']} {what}", n))
    for i, n in runs(lambda c: c["action"], lambda c: c["error"]):
        if n >= a.error_repeat:
            c = acts[i + a.error_repeat - 1]
            hits.append(_hit("error", c["t"], f"{c['name']} {c['cmd'][:80]} (rc {c['rc']})", n))
    # 只说话：连续的模型回复都没有工具调用
    k, start = 0, None
    for c in calls + [{"talk": False, "t": None}]:
        if c["talk"]:
            k += 1
            if k == a.monologue:
                start = c["t"]
        else:
            if k >= a.monologue:
                hits.append(_hit("monologue", start, f"{k} replies in a row without a tool call", k))
            k = 0
    # 交替：A B A B …（动作 + 观察）
    pair = [(c["action"], c["obs"]) for c in acts]
    i = 0
    while i + a.alternate <= len(pair):
        w = pair[i:i + a.alternate]
        if w[0] != w[1] and all(w[k] == w[k + 2] for k in range(len(w) - 2)):
            j = i + a.alternate
            while j < len(pair) and pair[j] == pair[j - 2]:
                j += 1
            c = acts[i + a.alternate - 1]
            hits.append(_hit("alternate", c["t"], f"{acts[i]['name']} / {acts[i + 1]['name']}", j - i))
            i = j
        else:
            i += 1
    # 同一个失败签名：改过文件之后再跑，还是同样失败
    streak, sig, wrote, first_t = 0, None, False, None
    for c in acts:
        if c["write"]:
            wrote = True
            continue
        if c["name"] != "bash" or not is_check(c["cmd"]):
            continue
        if not c["error"]:
            if streak >= a.same_fail:
                hits.append(_hit("samefail", first_t, f"{sig}", streak))
            streak, sig, wrote = 0, None, False
            continue
        s = failure_signature(c["out"])
        if s == sig and wrote:
            streak += 1
            if streak == a.same_fail:
                first_t = c["t"]
        elif s != sig:
            if streak >= a.same_fail:
                hits.append(_hit("samefail", first_t, f"{sig}", streak))
            streak, sig = 1, s
        wrote = False
    if streak >= a.same_fail:
        hits.append(_hit("samefail", first_t, f"{sig}", streak))
    return hits


def detect_revisits(snaps: list, a) -> list[dict]:
    """同一会话里工作区回到了出现过的树（中间有别的树）。同一棵树出现 ≥ revisits+1 次才报。"""
    seq: list[tuple[float, str, str]] = []
    for e in snaps:
        tree = e.get("raw_tree") or e.get("tree")
        if not seq or seq[-1][1] != tree:
            seq.append((e.t, tree, e.get("reason") or ""))
    seen: Counter = Counter()
    first: dict[str, tuple[float, str]] = {}
    for t, tree, reason in seq:
        seen[tree] += 1
        if seen[tree] == a.revisits + 1:
            first[tree] = (t, reason)
    return [_hit("revisit", t, f"tree {tree[:10]} seen {seen[tree]}x (reason when it came back: {reason or '-'})",
                 seen[tree]) for tree, (t, reason) in sorted(first.items(), key=lambda kv: kv[1][0])]


# ======================================================================== 一次运行

def analyse(bdir: Path, a) -> tuple[list[str], dict]:
    events = load_events(bdir)
    if not events:
        return [f"## {bdir}", "", "（事件日志是空的）", ""], {}
    t0 = next((e.t for e in events if e.type == "clock_started"), events[0].t)
    end_t = next((e.t for e in events if e.type == "finalize_started"), events[-1].t)
    rel = lambda t: mmss(None if t is None else t - t0)          # noqa: E731
    prog, how = progress_times(events)
    improve_t = next((e.t for e in events if e.type == "improve_started"), None)
    mode = next((e.get("mode") for e in events if e.type == "improve_started"), None)
    extra = re.compile(a.test_re) if a.test_re else None

    def is_check(cmd: str) -> bool:
        if extra and extra.search(cmd):
            return True
        return is_test_command(cmd) if a.tests_only else is_run_command(cmd)

    def phase(t: Optional[float]) -> str:
        return "POLISH" if improve_t is not None and t is not None and t >= improve_t else "需求"

    sessions = {}
    for e in events:
        if e.type == "session_started":
            sessions[e.get("session")] = {"id": e.get("session"), "start": e.t, "end": None, "reason": e.get("reason"),
                                          "end_reason": None}
        elif e.type == "session_ended" and e.get("session") in sessions:
            sessions[e.get("session")].update(end=e.t, end_reason=e.get("reason"))
    for s in sessions.values():
        if s["end"] is None:
            s["end"], s["end_reason"] = end_t, "(run end)"

    def next_progress(t: float, until: float) -> Optional[float]:
        return next((p for p in prog if t < p <= until), None)

    lines = [f"## {bdir}", ""]
    lines.append(f"- 计时开始 {rel(t0)}，收尾 {rel(end_t)}；会话 {len(sessions)} 个；进展 {len(prog)} 次（{how}）"
                 + (f"；POLISH（{mode}）从 {rel(improve_t)} 开始" if improve_t else "；没有进入 POLISH"))
    lines.append("")

    # ---- 一、会话内的模式 + 三、命中之后
    all_hits = []
    snaps_by_session = defaultdict(list)
    for e in events:
        if e.type == "snapshot_taken" and e.get("session"):
            snaps_by_session[e.get("session")].append(e)
    files = {f"S{m.group(1)}": p for p in (bdir / "sessions").glob("S*.jsonl")
             if (m := SESSION_FILE.match(p.name))} if (bdir / "sessions").is_dir() else {}
    per_session = []
    for sid, s in sorted(sessions.items(), key=lambda kv: kv[1]["start"]):
        calls = []
        if sid in files:
            calls, _talk = tool_calls(files[sid])
        hits = detect_calls(calls, a, is_check) + detect_revisits(snaps_by_session.get(sid, []), a)
        for h in hits:
            h["session"] = sid
            esc = next_progress(h["t"], s["end"]) if h["t"] is not None else None
            h["escape"] = None if esc is None else esc - h["t"]
            h["after"] = "自己走出来" if esc is not None else f"会话结束（{s['end_reason']}）"
            h["phase"] = phase(h["t"])
        all_hits += hits
        acts = [c for c in calls if not c["talk"]]
        per_session.append([sid, s["reason"], rel(s["start"]), rel(s["end"]), s["end_reason"], len(acts),
                            sum(1 for c in acts if c["write"]),
                            sum(1 for c in acts if c["name"] == "bash" and is_check(c["cmd"])),
                            sum(1 for p in prog if s["start"] <= p <= s["end"]), len(hits)])
    lines.append("### 会话")
    lines += table(["会话", "开场理由", "开始", "结束", "结束原因", "工具调用", "改文件", "跑测试/程序", "进展", "命中"],
                   per_session)
    lines.append("")
    lines.append("### 一、会话内的打转模式（命中之后有没有自己走出来）")
    if all_hits:
        rows = [[h["session"], h["phase"], h["kind"], rel(h["t"]), h["n"], h["detail"][:90], h["after"],
                 minutes(h["escape"])] for h in sorted(all_hits, key=lambda h: h["t"] or 0)]
        lines += table(["会话", "阶段", "模式", "命中时刻", "长度", "内容", "之后", "多久之后有进展(分)"], rows)
    else:
        lines.append("（没有命中）")
    lines.append("")

    # ---- 二、无进展窗口
    windows = []
    for sid, s in sessions.items():
        marks = [s["start"]] + [p for p in prog if s["start"] < p <= s["end"]]
        for i, st in enumerate(marks):
            nxt = marks[i + 1] if i + 1 < len(marks) else None
            stop = nxt if nxt is not None else s["end"]
            windows.append({"session": sid, "start": st, "len": stop - st, "escaped": nxt is not None,
                            "how": "有了进展" if nxt is not None else f"会话结束（{s['end_reason']}）",
                            "phase": phase(st)})
    long = sorted((w for w in windows if w["len"] >= a.min_window * 60), key=lambda w: -w["len"])
    lines.append(f"### 二、无进展窗口（≥ {a.min_window} 分钟）")
    if long:
        rows = []
        for w in long:
            inside = [h["kind"] for h in all_hits if h["session"] == w["session"] and h["t"] is not None
                      and w["start"] <= h["t"] <= w["start"] + w["len"]]
            rows.append([w["session"], w["phase"], rel(w["start"]), minutes(w["len"]), w["how"],
                         ", ".join(f"{k}×{n}" for k, n in Counter(inside).items()) or "-"])
        lines += table(["会话", "阶段", "开始", "时长(分)", "怎么结束的", "窗口里的命中"], rows)
    else:
        lines.append("（没有）")
    lines.append("")

    # ---- 四、运行时已有的信号
    lines.append("### 四、运行时已有的信号")
    stalls = [e for e in events if e.type == "stall_detected"]
    if stalls:
        lines += table(["时刻", "类型", "动作", "说明"], [[rel(e.t), e.get("kind"), e.get("action"),
                                                         str(e.get("detail") or "")[:120]] for e in stalls])
    else:
        lines.append("stall_detected：没有")
    lines.append("")
    # 复核者每一次的判定（review_decided，含没被合并的复核）：同一需求 / 改进项连续被判为 partial / not_done
    trig = {e.get("review"): e.get("trigger") for e in events if e.type == "review_started"}
    streaks = []
    cur: dict[tuple, list] = defaultdict(list)
    best: dict[tuple, list] = {}
    for e in events:
        if e.type != "review_decided":
            continue
        imp = e.get("improvements") or {}
        items = [("需求", j.get("requirement"), j) for j in e.get("judgements") or []] + \
                [("改进项", j.get("improvement"), j) for j in (imp.get("judged") or [] if isinstance(imp, dict) else [])]
        for kind, k, j in items:
            if j.get("status") == "open" and j.get("judgement") in ("partial", "not_done"):
                cur[(kind, k)].append(trig.get(e.get("review"), "?"))
                if len(cur[(kind, k)]) > len(best.get((kind, k), [])):
                    best[(kind, k)] = list(cur[(kind, k)])
            else:
                cur[(kind, k)] = []
    for (kind, k), tr in best.items():
        if len(tr) >= 2:
            streaks.append([kind, k, len(tr), ", ".join(f"{t}×{n}" for t, n in Counter(tr).items())])
    no_change = {e.get("submit") for e in events if e.type == "submit_requested" and e.get("attempt") is None}
    submit_attempts = {e.get("attempt") for e in events if e.type == "merge_requested" and e.get("trigger") == "submit"}
    sub_end = Counter()
    for e in events:
        if e.type == "submit_updated" and e.get("status") in ("accepted", "returned"):
            sub_end[e.get("status") + ("（没有新改动）" if e.get("submit") in no_change else "")] += 1
        elif e.type == "merge_rejected" and e.get("attempt") in submit_attempts:
            sub_end[f"rejected（{e.get('reason')}）"] += 1
    lines.append("submit 的结局：" + ("，".join(f"{k} {n}" for k, n in sub_end.items()) or "没有 submit"))
    lines.append("")
    if streaks:
        lines.append("被复核者连续判为没做完（≥2 次；含没被合并的复核，按复核的触发分；没有新改动又 submit 而被交还的"
                     "不经复核者，见上面的 stall_detected 与 submit 的结局）：")
        lines += table(["类别", "编号", "最长连续", "触发"], sorted(streaks, key=lambda r: -r[2]))
    else:
        lines.append("没有被复核者连续两次以上判为没做完的需求 / 改进项")
    lines.append("")
    merges = [e for e in events if e.type == "merged" and e.get("attempt")]
    last_merge = merges[-1].t if merges else None
    req = {e.get("attempt"): e for e in events if e.type == "merge_requested"}
    dec = {e.get("review"): e for e in events if e.type == "review_decided"}
    att_review = {e.get("attempt"): e.get("review") for e in events if e.type == "review_started" and e.get("attempt")}
    after = []
    for e in events:
        if e.type == "merge_rejected" and (last_merge is None or e.t > last_merge):
            r = req.get(e.get("attempt"))
            d = dec.get(att_review.get(e.get("attempt")))
            why = e.get("reason")
            if why == "review" and d is not None:
                why += " / " + (",".join(d.get("blockers") or []) or "no blocker")
            regs = ", ".join(str(x) for x in (e.get("regressions") or [])[:3])
            after.append([rel(e.t), r.get("trigger") if r else "?", r.get("lane") if r else "?", why,
                          (regs or str(e.get("detail") or ""))[:100]])
    lines.append(f"最后一个合并点在 {rel(last_merge)}，之后到收尾还有 {minutes(None if last_merge is None else end_t - last_merge)} 分钟；"
                 f"这段时间里被拒的合并请求 {len(after)} 个：")
    if after:
        lines += table(["时刻", "触发", "车道", "原因", "回归 / 说明"], after)
    lines.append("")

    data = {"run": str(bdir), "progress_def": how, "polish_start": None if improve_t is None else improve_t - t0,
            "hits": [{k: v for k, v in h.items()} for h in all_hits],
            "windows": windows, "stalls": [{"t": e.t - t0, "kind": e.get("kind"), "action": e.get("action")}
                                           for e in stalls],
            "streaks": streaks, "rejected_after_last_merge": len(after),
            "minutes_after_last_merge": None if last_merge is None else (end_t - last_merge) / 60}
    for h in data["hits"]:
        h["t"] = None if h["t"] is None else h["t"] - t0
    for w in data["windows"]:
        w["start"] -= t0
    return lines, data


def summary(datas: list[dict], a) -> list[str]:
    lines = ["# 汇总", ""]
    hits = [h for d in datas for h in d.get("hits", [])]
    if hits:
        rows = []
        for kind in ("repeat", "error", "monologue", "alternate", "samefail", "revisit"):
            hs = [h for h in hits if h["kind"] == kind]
            if not hs:
                rows.append([kind, 0, "-", "-", "-"])
                continue
            esc = [h["escape"] for h in hs if h["escape"] is not None]
            rows.append([kind, len(hs), f"{len(esc)}/{len(hs)}",
                         minutes(sorted(esc)[len(esc) // 2]) if esc else "-",
                         ", ".join(f"{p}×{n}" for p, n in Counter(h["phase"] for h in hs).items())])
        lines += table(["模式", "命中", "之后自己走出来", "多久之后有进展（中位，分）", "阶段"], rows)
    else:
        lines.append("会话内的打转模式：所有运行都没有命中。")
    lines.append("")
    ws = [w for d in datas for w in d.get("windows", [])]
    lines.append("无进展窗口的“生存表”：卡住 ≥ t 分钟的窗口里，后来自己有了进展的比例（同一会话内）")
    rows = []
    for t in (5, 10, 15, 20, 30, 45, 60):
        reach = [w for w in ws if w["len"] >= t * 60]
        esc = [w for w in reach if w["escaped"]]
        rows.append([t, len(reach), len(esc), f"{len(esc) / len(reach):.0%}" if reach else "-",
                     ", ".join(f"{p}×{n}" for p, n in Counter(w["phase"] for w in reach).items()) or "-"])
    lines += table(["t（分）", "窗口数", "自己走出来", "比例", "阶段"], rows)
    lines.append("")
    lines.append("注：一个窗口结束于“会话结束”不一定是坏事（可能是交接、提交被接受、进入 POLISH），看分运行的表里的结束原因。")
    lines.append("")
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("targets", nargs="+", help="run_id、trial 目录或 agent/belay 目录")
    ap.add_argument("--repeat", type=int, default=4)
    ap.add_argument("--error-repeat", type=int, default=3)
    ap.add_argument("--monologue", type=int, default=3)
    ap.add_argument("--alternate", type=int, default=6)
    ap.add_argument("--same-fail", type=int, default=3)
    ap.add_argument("--revisits", type=int, default=2)
    ap.add_argument("--min-window", type=float, default=10, help="列出的无进展窗口的最短时长（分钟）")
    ap.add_argument("--test-re", default="", help="额外算作“测试 / 程序”的命令（正则）")
    ap.add_argument("--tests-only", action="store_true", help="samefail 只看测试命令（默认测试与普通运行命令都算）")
    ap.add_argument("--out", help="报告另存为 markdown 文件")
    ap.add_argument("--json", help="明细另存为 json 文件")
    a = ap.parse_args(argv)
    lines, datas = [], []
    for target in a.targets:
        for _trial, bdir in find_targets(target):
            ls, d = analyse(bdir, a)
            lines += ls
            if d:
                datas.append(d)
    text = "\n".join(summary(datas, a) + lines)
    print(text)
    if a.out:
        Path(a.out).write_text(text + "\n", encoding="utf-8")
    if a.json:
        Path(a.json).write_text(json.dumps(datas, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
