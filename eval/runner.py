"""执行 RunPlan：对每个 (题目, 第 k 次) 先跑 agent 拿补丁，再在全新容器里重放评分。

结果目录：<results_root>/<run_id>/<benchmark>/<id>/<repeat>/
    agent_job.yaml  agent_pier.log  pier/agent/<trial>/...   Pier 原始输出（轨迹、日志）
    patch.diff      run.json                                 agent 阶段的产物与指标
    grade_job.yaml  grade_pier.log  pier/grade/<trial>/...   重放评分的原始输出
    grade.json                                               评分结果
run.json / grade.json 的 status 为 done 时视为完成，重跑同一命令会自动跳过（断点续跑）。
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from eval import pier_backend as pb
from eval import report
from eval.config import RunPlan, Step, TaskRef

_print_lock = threading.Lock()

# 这些数据集的评分脚本只评"已提交"的内容，重放时应用补丁后需要 commit（与其 solve.sh 一致）
COMMIT_SUBMISSION = {"deepswe"}


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def read_json(p: Path) -> dict:
    return json.loads(p.read_text()) if p.exists() else {}


def write_json(p: Path, obj: dict) -> None:
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def patch_stats(path: Path) -> dict:
    text = path.read_text(errors="ignore") if path.exists() else ""
    files = re.findall(r"^diff --git a/(\S+) b/", text, flags=re.M)
    loc = sum(1 for l in text.splitlines() if l[:1] in "+-" and not l.startswith(("+++", "---")))
    return {"patch_files": len(files), "patch_loc": loc, "patch_bytes": len(text.encode())}


# ---------------------------------------------------------------- 运行前检查
def preflight(plan: RunPlan) -> list[str]:
    """返回问题列表；为空才开始跑。"""
    problems = []
    uses_model = any(s.agent.get("name") not in ("oracle", "nop") and s.agent_key != "replay"
                     for s in plan.steps)
    for t in plan.tasks:
        d = plan.task_dir(t)
        toml_path = d / "task.toml"
        if not toml_path.exists():
            problems.append(f"{t.key}: 任务目录不完整（缺 {toml_path}），先运行 python -m eval.prepare")
            continue
        for need in ("instruction.md", "tests/test.sh"):
            if not (d / need).exists():
                problems.append(f"{t.key}: 缺少 {need}")
        if uses_model:
            env = tomllib.loads(toml_path.read_text()).get("environment", {})
            public = env.get("network_mode") == "public" or (
                "network_mode" not in env and env.get("allow_internet", True))
            if public:
                problems.append(f"{t.key}: task.toml 允许联网；防泄漏要求 [environment] allow_internet = false"
                                f"（Pier 会只放行 agent 声明的模型 API 域名）")
    if plan.grading_only:
        for t in plan.tasks:
            if not any((plan.results_root / plan.source_run / t.benchmark / t.id).glob("*/patch.diff")):
                problems.append(f"{t.key}: source_run {plan.source_run} 中没有补丁")
    return problems


# ---------------------------------------------------------------- 两个阶段
def agent_phase(plan: RunPlan, step: Step, t: TaskRef, d: Path) -> dict:
    verify_inline = plan.inline_verify or step.grade_mode == "inline"
    cfg = pb.job_config(job_name="agent", jobs_dir=d / "pier", task_dir=plan.task_dir(t),
                        agent_cfg=pb.agent_config(step.agent, plan.timeout_min),
                        environment=plan.environment, keep_container=plan.keep_containers,
                        verify=verify_inline)
    out = pb.run_job(cfg, d / "agent_job.yaml", d / "agent_pier.log",
                     timeout_sec=plan.timeout_min * 60 + 3600)
    rec: dict = {"task": t.key, "agent": step.agent_key, "model": step.agent.get("model"),
                 "pier_rc": out.returncode, "pier_trial_dir": str(out.trial_dir or "")}
    if not out.ok:
        rec.update(status="error", error=f"Pier 没有产出 trial 结果，见 {out.log_path}")
        return rec

    s = pb.summarize_result(out.result)
    rec.update(s)
    rec["inline_resolved"], rec["inline_fix_rate"] = pb.interpret_rewards(s["rewards"])

    if step.grade_mode == "replay":
        src = out.trial_dir / "agent" / "patch.diff"
        if not src.exists():
            rec.update(status="error", error="agent 没有导出 patch.diff（自定义 agent 需继承 PatchCaptureMixin）")
            return rec
        shutil.copy(src, d / "patch.diff")
        rec.update(patch_stats(d / "patch.diff"))
    rec["status"] = "done"
    return rec


def grade_phase(plan: RunPlan, t: TaskRef, d: Path, patch: Path) -> dict:
    cfg = pb.job_config(job_name="grade", jobs_dir=d / "pier", task_dir=plan.task_dir(t),
                        agent_cfg={"import_path": pb.REPLAY_AGENT,
                                   "kwargs": {"patch_path": str(patch.resolve()),
                                              "commit": t.benchmark in COMMIT_SUBMISSION}},
                        environment=plan.environment, keep_container=False, verify=True)
    out = pb.run_job(cfg, d / "grade_job.yaml", d / "grade_pier.log", timeout_sec=3 * 3600)
    if not out.ok:
        return {"status": "error", "error": f"评分 trial 未产出结果，见 {out.log_path}"}
    s = pb.summarize_result(out.result)
    if not s["rewards"]:
        return {"status": "error", "error": f"verifier 没有给出 reward（{s['exception']}），见 {out.trial_dir}"}
    resolved, fix = pb.interpret_rewards(s["rewards"])
    apply = read_json(out.trial_dir / "agent" / "apply.json")
    return {"status": "done", "resolved": resolved, "fix_rate": fix, "rewards": s["rewards"],
            "apply_ok": apply.get("ok"), "apply_method": apply.get("method"),
            "committed": apply.get("committed"),
            "verify_sec": s["total_sec"]}


def run_trial(plan: RunPlan, step: Step, t: TaskRef, repeat: int) -> dict:
    d = plan.trial_dir(step, t, repeat)
    d.mkdir(parents=True, exist_ok=True)
    run_path, grade_path = d / "run.json", d / "grade.json"

    rec = read_json(run_path)
    if plan.grading_only:
        src = plan.source_trial_dir(t, repeat) / "patch.diff"
        if src.exists() and rec.get("status") != "done":
            shutil.copy(src, d / "patch.diff")
            rec = {"task": t.key, "source": str(src), "status": "done", **patch_stats(d / "patch.diff")}
            write_json(run_path, rec)
        elif not src.exists():
            return {"status": "skipped", "error": f"没有 {src}"}
    elif rec.get("status") != "done":
        log(f"  ▶ {t.key} #{repeat} agent 开始")
        rec = agent_phase(plan, step, t, d)
        write_json(run_path, rec)

    grade = read_json(grade_path)
    if step.grade_mode == "replay" and rec.get("status") == "done" and grade.get("status") != "done":
        log(f"  ▶ {t.key} #{repeat} 重放评分")
        grade = grade_phase(plan, t, d, d / "patch.diff")
        write_json(grade_path, grade)
    return {**rec, "grade": grade}


def final_resolved(step: Step, rec: dict) -> bool | None:
    return rec.get("grade", {}).get("resolved") if step.grade_mode == "replay" else rec.get("inline_resolved")


# ---------------------------------------------------------------- 主流程
def execute(plan: RunPlan) -> bool:
    all_ok = True
    for step in plan.steps:
        root = plan.results_root / step.run_id
        root.mkdir(parents=True, exist_ok=True)
        write_json(root / "plan.json", {
            "profile": plan.profile, "split": plan.split, "agent_key": step.agent_key,
            "agent": {k: v for k, v in step.agent.items() if k != "env"},
            "env_keys": sorted((step.agent.get("env") or {})), "repeats": step.repeats,
            "timeout_min": plan.timeout_min, "tasks": [t.key for t in plan.tasks]})

        if plan.grading_only:     # 重新评分：源 run 里有几次就评几次
            jobs = [(t, int(d.name)) for t in plan.tasks
                    for d in sorted((plan.results_root / plan.source_run / t.benchmark / t.id).glob("*"))
                    if d.name.isdigit() and (d / "patch.diff").exists()]
        else:
            jobs = [(t, r) for t in plan.tasks for r in range(1, step.repeats + 1)]
        log(f"\n== {step.run_id}：{len(jobs)} 个 trial，并发 {plan.concurrency}，结果目录 {root}")
        with ThreadPoolExecutor(max_workers=plan.concurrency) as ex:
            futs = {ex.submit(run_trial, plan, step, t, r): (t, r) for t, r in jobs}
            for f in as_completed(futs):
                t, r = futs[f]
                try:
                    rec = f.result()
                except Exception as e:                       # 单个 trial 崩溃不影响其他
                    rec = {"status": "crash", "error": repr(e)}
                res = final_resolved(step, rec)
                mark = {True: "✅", False: "❌", None: "⚠️ "}[res]
                extra = rec.get("error") or rec.get("grade", {}).get("error") or ""
                log(f"  {mark} {t.key} #{r}  status={rec.get('status')}  "
                    f"agent={rec.get('agent_sec')}s  exc={rec.get('exception')}  {extra}")

        rows = report.collect(plan, step)
        report.write(root, rows)
        log(f"   汇总：{root / 'summary.md'}")

        if step.expect:                                      # gold-check 的期望检查
            want = step.expect == "pass"
            bad = [r for r in rows if r["resolved"] is not want]
            if bad:
                all_ok = False
                log(f"   ✗ 期望全部 {step.expect}，不符合：" + ", ".join(f"{r['task']}#{r['repeat']}" for r in bad))
            else:
                log(f"   ✓ 全部符合期望 {step.expect}")
    return all_ok
