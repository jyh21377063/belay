"""离线评估申诉机制：门禁拦下的旧测试，如果让 worker 申诉、由独立评审裁决，能判对多少？

  python -m eval.tools.appeal_eval label  --gate-run diag-gate-swe-v2
  python -m eval.tools.appeal_eval review --gate-run diag-gate-swe-v2

label（容器内跑测试，不联网，不调模型）：对 A-gate 拦过的每个测试（仓库里的旧版本），在三种代码上各跑一次：
  orig   原始代码          应当通过（它在门禁基线里）；不通过记为 invalid
  gold   参考解            通过 → 这项行为本该保持，申诉应驳回（preserve）
                           失败 → release 确实改变了它，申诉应批准（change）
  final  agent 的最终补丁  （已剔除测试改动）仍失败的，才是 agent 需要申诉的；同时取得失败输出
review（调模型）：对 final 仍失败、label 有效的测试，模拟完整的申诉流程：
  1. 申诉方：LLM 扮演想通过门禁的 worker，看任务原文、测试源码、失败输出、diff，写理由与原文引文（可以放弃申诉）
  2. 规则预检：引文逐字在任务原文中；引文里的标识符至少一个出现在测试源码或失败输出里
  3. 评审：独立上下文的 LLM（沿用 belay/runtime/prompts.py 的 REVIEWER_SYSTEM），裁决 approve / reject 并给引文
  4. 规则复核评审的引文
  与 label 对比，输出混淆矩阵；最危险的是"批准了本该保持的行为"（放过真回归）。

需要：宿主机能执行 docker（会用任务目录里的 Dockerfile 构建评测镜像，构建时需要联网装依赖，Pier 构建过则走缓存）；review 需要环境变量 DEEPSEEK_API_KEY（可选 ANTHROPIC_BASE_URL）。
输出：<results>/appeal-eval/<gate-run>/cases.json、reviews.jsonl、summary.md
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

from belay.graph.requirements import quote_in
from belay.runtime.prompts import REVIEWER_SYSTEM, reviewer_message
from eval.tools.oracle_failures import parse

REPO = Path(__file__).resolve().parents[2]
ROOT = REPO.parent
OK = ("PASSED", "XFAIL")

ADVOCATE_SYSTEM = """You are the IMPLEMENTER of a coding task. A regression gate refuses to accept your work because \
an existing test, which passed on the original code, now fails. You can file a report_conflict claiming that the task \
explicitly asks for the behaviour change that makes this test fail. A reviewer will decide, and only approves when the \
task text explicitly describes that change.

Make the strongest HONEST case you can. You must quote, verbatim, the passage of the task text that asks for the \
change. If, after reading the test and the failure, you believe the failure is simply a bug in your change, or you \
cannot find such a passage, do not appeal.

Reply with a JSON object only:
{"appeal": true | false, "quote": "<verbatim passage of the task text, or empty>", "reason": "<two or three sentences>"}"""

ADVOCATE_USER = """<task_text>
{task}
</task_text>

<failing_test id="{test}">
{source}
</failing_test>

<failure_output>
{failure}
</failure_output>

<your_diff>
{diff}
</your_diff>"""


# ---- 读取已有运行 ---------------------------------------------------------------

def blocked_tests(gate_run: Path, tid: str) -> list[str]:
    files = sorted((gate_run / "swe_evo" / tid).glob("1/pier/**/gate/checks.jsonl"))
    out: list[str] = []
    for f in files[:1]:
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("decision") == "block":
                out += [t for t in rec.get("regressions") or [] if t not in out]
    return out


def ensure_image(task_dir: Path, tid: str, timeout: int) -> str | None:
    """用任务目录里的 Dockerfile 构建评测时用的镜像（切到起始版本、装依赖）；docker 有缓存，Pier 构建过就很快。
    不能直接用 tasks.yaml 里的基础镜像：它原本属于另一道题，代码版本不对。"""
    tag = "belay-appeal:" + re.sub(r"[^A-Za-z0-9_.-]", "_", tid)[:120]
    r = subprocess.run(["docker", "build", "-q", "-t", tag, str(task_dir / "environment")],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        print(f"   构建镜像失败：{(r.stderr or r.stdout)[-500:]}")
        return None
    return tag


def final_patch(results: Path, gate_run: str, tid: str) -> str:
    for run in (f"{gate_run}-notests", gate_run):
        p = results / run / "swe_evo" / tid / "1" / "patch.diff"
        if p.exists():
            return p.read_text(errors="replace")
    return ""


# ---- 容器内跑测试 ---------------------------------------------------------------

def run_in_image(image: str, gate: dict, patch: str, tests: list[str], timeout: int) -> tuple[str, dict[str, str]]:
    """在全新容器里（不联网）应用补丁、跑指定测试；同时把这些测试文件的源码拷出来。返回（pytest 输出，{文件: 源码}）。"""
    files = sorted({t.split("::")[0] for t in tests})
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "p.diff").write_text(patch)
        apply = ("if [ -s /work/p.diff ]; then git apply --whitespace=nowarn /work/p.diff || "
                 "patch --batch -p1 -i /work/p.diff || echo 'PATCH FAILED'; fi")
        script = "; ".join([
            f"cd {shlex.quote(gate.get('workdir', '/testbed'))}",
            gate.get("prelude") or "true",
            *(gate.get("commands") or []),
            apply,
            "mkdir -p /work/src && cp --parents " + " ".join(shlex.quote(f) for f in files) + " /work/src/ 2>/dev/null",
            "python -m pytest -rA -p no:cacheprovider " + " ".join(shlex.quote(t) for t in tests)
            + " > /work/out.txt 2>&1",
            "chmod -R a+rwX /work",
        ])
        cmd = ["docker", "run", "--rm", "--network", "none", "-v", f"{tmp}:/work", "--entrypoint", "bash",
               image, "-c", script]
        try:
            subprocess.run(cmd, timeout=timeout, capture_output=True, text=True)
        except subprocess.TimeoutExpired:
            return "TIMEOUT", {}
        out = Path(tmp, "out.txt").read_text(errors="replace") if Path(tmp, "out.txt").exists() else ""
        srcs = {f: Path(tmp, "src", f).read_text(errors="replace") for f in files if Path(tmp, "src", f).exists()}
        return out, srcs


def failure_section(out: str, test: str, limit: int = 2500) -> str:
    """从 pytest 输出里取某个测试的失败详情（____ name ____ 段落），取不到时退回简短摘要行。"""
    name = test.split("::", 1)[1].replace("::", ".") if "::" in test else test
    m = re.search(r"(?m)^_{3,} " + re.escape(name) + r" _{3,}\n(.*?)(?=^_{3,} |^={3,})", out, re.S)
    if m:
        body = m.group(1).strip()
        return body if len(body) <= limit else "...\n" + body[-limit:]
    line = next((l for l in out.splitlines() if l.startswith(("FAILED", "ERROR")) and test in l), "")
    return line or "(no failure details captured)"


def test_source(srcs: dict[str, str], test: str, limit: int = 4000) -> str:
    """测试函数的源码：按缩进截取 def 块（含装饰器与所在类名）；找不到时给文件开头。"""
    f = test.split("::")[0]
    src = srcs.get(f, "")
    if not src:
        return "(source not available)"
    fn = re.sub(r"\[.*\]$", "", test.split("::")[-1])
    lines = src.splitlines()
    for i, l in enumerate(lines):
        m = re.match(r"(\s*)(async\s+)?def " + re.escape(fn) + r"\b", l)
        if not m:
            continue
        ind, j = len(m.group(1)), i
        while j > 0 and lines[j - 1].strip().startswith("@"):
            j -= 1
        k = i + 1
        while k < len(lines) and (not lines[k].strip() or len(lines[k]) - len(lines[k].lstrip()) > ind):
            k += 1
        block = "\n".join(lines[j:k])
        return f"# {f}\n{block[:limit]}"
    return f"# {f} (function not found)\n" + "\n".join(lines[:120])[:limit]


def label_of(orig: str | None, gold: str | None, gold_ran: bool = True) -> str:
    """gold 为 None（没跑出结果）时：参考解整体跑出了结果，说明是它让这个旧测试收集失败，算 change；否则 invalid。"""
    if orig not in OK or not gold_ran:
        return "invalid"
    return "preserve" if gold in OK else "change"


def cmd_label(a) -> int:
    results, gate_run = a.results, a.results / a.gate_run
    out_dir = results / "appeal-eval" / a.gate_run
    out_dir.mkdir(parents=True, exist_ok=True)
    cases = []
    for tdir in sorted((gate_run / "swe_evo").glob("*")):
        tid = tdir.name
        tests = blocked_tests(gate_run, tid)
        if not tests:
            continue
        task_dir = a.task_dirs / "swe_evo" / tid
        gate = json.loads((task_dir / "gate.json").read_text())
        print(f"== {tid}: 构建评测镜像……")
        image = ensure_image(task_dir, tid, a.build_timeout)
        if not image:
            continue
        gold = (task_dir / "solution" / "gold.patch").read_text(errors="replace")
        final = final_patch(results, a.gate_run, tid)
        print(f"== {tid}: {len(tests)} 个被拦过的测试")
        runs = {}
        for name, patch in (("orig", ""), ("gold", gold), ("final", final)):
            out, srcs = run_in_image(image, gate, patch, tests, a.timeout)
            if "PATCH FAILED" in out:
                print(f"   {name}: 补丁应用失败")
            runs[name] = (parse(out)[0], out, srcs)
            print(f"   {name}: 通过 {sum(1 for t in tests if runs[name][0].get(t) in OK)}/{len(tests)}")
        instruction = (task_dir / "instruction.md").read_text(errors="replace")
        for t in tests:
            st = {k: runs[k][0].get(t) for k in runs}
            cases.append({"task": tid, "test": t, **st, "label": label_of(st["orig"], st["gold"], bool(runs["gold"][0])),
                          "needs_appeal": st["final"] not in OK,
                          "failure": failure_section(runs["final"][1], t) if st["final"] not in OK else "",
                          "source": test_source(runs["orig"][2], t)})
        (out_dir / f"instruction-{tid}.md").write_text(instruction)
        (out_dir / f"final-{tid}.diff").write_text(final)
    (out_dir / "cases.json").write_text(json.dumps(cases, indent=1, ensure_ascii=False))
    c = Counter((x["label"], x["needs_appeal"]) for x in cases)
    print(f"\n写入 {out_dir / 'cases.json'}：共 {len(cases)} 个")
    for (lab, need), n in sorted(c.items()):
        print(f"   label={lab:<9} final 仍失败={need!s:<5} {n}")
    return 0


# ---- 模拟申诉与裁决 ---------------------------------------------------------------

def llm_json(client, model: str, system: str, user: str) -> tuple[dict, str]:
    resp = client.messages.create(model=model, max_tokens=4000, system=system,
                                  messages=[{"role": "user", "content": user}])
    text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return (json.loads(m.group(0)) if m else {}), text
    except json.JSONDecodeError:
        return {}, text


def identifiers(quote: str) -> list[str]:
    """引文里像代码标识符的词：反引号内容、带下划线或点、驼峰、以 -- 开头的选项。"""
    ids = re.findall(r"`([^`]+)`", quote)
    ids += re.findall(r"(?<![\w.])(--?[A-Za-z][\w-]+|[A-Za-z_]\w*[._]\w+|[a-z]+[A-Z]\w*)", quote)
    return sorted({i.strip() for i in ids if len(i.strip()) >= 3})


def rule_precheck(quote: str, task: str, source: str, failure: str) -> tuple[bool, str]:
    if not quote or not quote_in(quote, task):
        return False, "quote not found verbatim in the task text"
    ids = identifiers(quote)
    if ids and not any(i.split(".")[-1] in source or i.split(".")[-1] in failure for i in ids):
        return False, f"none of the identifiers in the quote ({', '.join(ids[:5])}) appear in the test or its failure"
    return True, ""


def relevant_diff(diff: str, source: str, failure: str, limit: int) -> str:
    """按与测试源码、失败输出的词重合度排序 diff 中的文件，截到 limit 个字符。"""
    words = set(re.findall(r"[A-Za-z_]\w{3,}", source + failure))
    blocks = [b for b in re.split(r"(?m)^(?=diff --git )", diff) if b.strip()]
    blocks.sort(key=lambda b: -len(words & set(re.findall(r"[A-Za-z_]\w{3,}", b))))
    out, n = [], 0
    for b in blocks:
        if n + len(b) > limit:
            out.append(b[: max(0, limit - n)] + "\n... (truncated)")
            break
        out.append(b)
        n += len(b)
    return "".join(out)


def cmd_review(a) -> int:
    from anthropic import Anthropic

    out_dir = a.results / "appeal-eval" / a.gate_run
    cases = json.loads((out_dir / "cases.json").read_text())
    todo = [c for c in cases if c["needs_appeal"] and c["label"] != "invalid"]
    if a.limit:
        todo = todo[: a.limit]
    client = Anthropic(api_key=os.environ["DEEPSEEK_API_KEY"],
                       base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic"))
    rows = []
    with open(out_dir / "reviews.jsonl", "w") as log:
        for i, c in enumerate(todo, 1):
            task = (out_dir / f"instruction-{c['task']}.md").read_text()
            diff = relevant_diff((out_dir / f"final-{c['task']}.diff").read_text(), c["source"], c["failure"],
                                 a.diff_chars)
            adv, adv_text = llm_json(client, a.model, ADVOCATE_SYSTEM, ADVOCATE_USER.format(
                task=task, test=c["test"], source=c["source"], failure=c["failure"], diff=diff))
            row = {**{k: c[k] for k in ("task", "test", "label")}, "appeal": adv, "stage": "", "decision": "reject"}
            if not adv.get("appeal"):
                row["stage"] = "no_appeal"
            else:
                ok, why = rule_precheck(adv.get("quote", ""), task, c["source"], c["failure"])
                if not ok:
                    row.update(stage="rule_precheck", rule_reason=why)
                else:
                    msg = reviewer_message("test_conflict", None, task, [c["test"]],
                                           f"{adv.get('reason', '')}\nQuoted task text: \"{adv.get('quote', '')}\"",
                                           diff, c["failure"], c["source"])
                    rev, rev_text = llm_json(client, a.model, REVIEWER_SYSTEM, msg)
                    row["review"] = rev
                    if rev.get("decision") == "approve" and not quote_in(rev.get("quote", ""), task):
                        row.update(stage="rule_recheck", rule_reason="reviewer quote not found verbatim")
                    else:
                        row.update(stage="reviewer", decision="approve" if rev.get("decision") == "approve" else "reject")
            row["correct"] = (row["decision"] == "approve") == (c["label"] == "change")
            rows.append(row)
            log.write(json.dumps(row, ensure_ascii=False) + "\n")
            log.flush()
            print(f"[{i}/{len(todo)}] {c['task'][:28]:<28} {c['test'].split('::')[-1][:40]:<40} "
                  f"label={c['label']:<8} → {row['decision']:<7} ({row['stage']}) {'✓' if row['correct'] else '✗'}")
    summarize(rows, out_dir)
    return 0


def summarize(rows: list[dict], out_dir: Path) -> None:
    conf = Counter((r["label"], r["decision"]) for r in rows)
    stages = Counter(r["stage"] for r in rows)
    lines = ["# 申诉离线评估", "",
             "| 应当（依据参考解） | 批准 | 驳回 |", "| --- | ---: | ---: |",
             f"| 批准（release 确实改了这项行为） | {conf[('change', 'approve')]} | {conf[('change', 'reject')]}（误拒） |",
             f"| 驳回（这项行为应当保持） | {conf[('preserve', 'approve')]}（**误放，会漏掉真回归**） | "
             f"{conf[('preserve', 'reject')]} |", "",
             "裁决落在哪一步：" + "，".join(f"{k} {v}" for k, v in stages.most_common()), "",
             "| 题目 | 测试 | 应当 | 结果 | 在哪一步 | 对错 |", "| --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        lines.append(f"| {r['task']} | `{r['test'].split('::', 1)[-1]}` | {r['label']} | {r['decision']} | "
                     f"{r['stage']} | {'✓' if r['correct'] else '✗'} |")
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n" + "\n".join(lines[:8]))
    print(f"\n明细：{out_dir / 'summary.md'}；每条的申诉与裁决原文：{out_dir / 'reviews.jsonl'}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m eval.tools.appeal_eval")
    p.add_argument("step", choices=["label", "review"])
    p.add_argument("--gate-run", required=True, help="A-gate 的 run_id（读取拦截记录与最终补丁）")
    p.add_argument("--results", type=Path, default=ROOT / "results")
    p.add_argument("--task-dirs", type=Path, default=ROOT / "tasks")
    p.add_argument("--timeout", type=int, default=1800, help="每次容器内测试的超时（秒）")
    p.add_argument("--build-timeout", type=int, default=3600, help="构建评测镜像的超时（秒）")
    p.add_argument("--model", default="deepseek-flash")
    p.add_argument("--diff-chars", type=int, default=15000)
    p.add_argument("--limit", type=int, default=0, help="只评审前 N 个（试跑用）")
    a = p.parse_args(argv)
    return cmd_label(a) if a.step == "label" else cmd_review(a)


if __name__ == "__main__":
    sys.exit(main())
