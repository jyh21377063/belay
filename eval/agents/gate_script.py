"""回归门（A-gate）：在容器内运行，只用标准库（兼容 Python 3.6+）。

  python3 gate_script.py baseline   # setup 阶段：原始代码上跑两次，记录两次都通过的测试
  python3 gate_script.py check      # Claude Code 的 Stop hook：重跑，若有"原来通过、现在失败"则拦下

配置 /opt/belay-gate/spec.json：
  workdir      运行测试的目录
  prelude      运行前的 shell 语句（如激活 conda 环境）
  commands     测试前需执行的命令列表
  test_cmd     测试命令；其中不存在的路径参数会被去掉（例如评分时才新增的测试文件）
  parser       parse_log_pytest | parse_log_pytest_pydantic
  timeout_sec  单次测试的超时
  max_blocks   最多拦截次数，之后放行

状态与记录：
  /opt/belay-gate/baseline.json（+ .sha256）  基线，放在 agent 工作区之外
  /opt/belay-gate/state/blocks                已拦截次数
  /logs/agent/gate/                           基线与每次检查的记录（同步到宿主机）
"""
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time

BASE = "/opt/belay-gate"
LOGDIR = "/logs/agent/gate"
STATUSES = ("FAILED", "PASSED", "SKIPPED", "ERROR", "XFAIL")
OK = ("PASSED", "XFAIL")


def parse_log_pytest(log):
    status = {}
    for line in log.split("\n"):
        if any(line.startswith(s) for s in STATUSES):
            if line.startswith("FAILED"):
                line = line.replace(" - ", " ")
            parts = line.split()
            if len(parts) > 1:
                status[parts[1]] = parts[0]
    return status


def parse_log_pytest_pydantic(log):
    status = {}
    table = str.maketrans("", "", "".join(chr(c) for c in range(1, 32)))
    for line in log.split("\n"):
        line = re.sub(r"\[(\d+)m", "", line).translate(table)
        line = re.sub(r"FAILED\s*\[.*?\]", "FAILED", line)
        if any(line.startswith(s) for s in STATUSES):
            if line.startswith("FAILED"):
                line = line.replace(" - ", " ")
            parts = line.split()
            if len(parts) > 1:
                status[parts[1]] = parts[0]
        elif any(line.endswith(s) for s in STATUSES):
            parts = line.split()
            if len(parts) > 1:
                status[parts[0]] = parts[1]
    return status


PARSERS = {"parse_log_pytest": parse_log_pytest, "parse_log_pytest_pydantic": parse_log_pytest_pydantic}


def load_spec():
    with open(os.path.join(BASE, "spec.json")) as f:
        return json.load(f)


def build_command(spec):
    """去掉不存在的路径参数（含 / 或以 .py 结尾、且不以 - 开头的参数）。"""
    kept, dropped = [], []
    for tok in shlex.split(spec["test_cmd"]):
        looks_like_path = not tok.startswith("-") and ("/" in tok or tok.endswith(".py"))
        path = tok.split("::")[0]
        if looks_like_path and not os.path.exists(os.path.join(spec["workdir"], path)):
            dropped.append(tok)
        else:
            kept.append(tok)
    has_target = any(not t.startswith("-") and ("/" in t or t.endswith(".py")) for t in kept)
    parts = [spec.get("prelude") or "true", "cd " + shlex.quote(spec["workdir"])] + list(spec.get("commands") or [])
    parts.append(" ".join(shlex.quote(t) for t in kept))
    return "; ".join(parts), has_target, dropped


def run_tests(spec):
    cmd, has_target, dropped = build_command(spec)
    if not has_target:
        return None, "no test files exist", dropped, ""
    t0 = time.time()
    try:
        out = subprocess.run(["bash", "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             timeout=int(spec.get("timeout_sec", 2400))).stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return None, "timeout after %ds" % (time.time() - t0), dropped, ""
    return PARSERS[spec.get("parser", "parse_log_pytest")](out), None, dropped, out


def log_event(name, obj):
    os.makedirs(LOGDIR, exist_ok=True)
    obj = dict(obj, ts=time.strftime("%Y-%m-%dT%H:%M:%S"))
    with open(os.path.join(LOGDIR, name), "a") as f:
        f.write(json.dumps(obj) + "\n")


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def baseline():
    spec = load_spec()
    runs = []
    for i in range(2):
        sm, err, dropped, _ = run_tests(spec)
        if sm is None:
            log_event("baseline.jsonl", {"run": i + 1, "error": err, "dropped": dropped})
            runs.append({})
        else:
            runs.append(sm)
            log_event("baseline.jsonl", {"run": i + 1, "n_tests": len(sm),
                                         "n_passed": sum(1 for s in sm.values() if s in OK), "dropped": dropped})
    stable = sorted(t for t in runs[0] if runs[0].get(t) in OK and runs[1].get(t) in OK)
    flaky = sorted(t for t in set(runs[0]) | set(runs[1]) if (runs[0].get(t) in OK) != (runs[1].get(t) in OK))
    path = os.path.join(BASE, "baseline.json")
    with open(path, "w") as f:
        json.dump({"stable_pass": stable, "flaky": flaky}, f)
    with open(path + ".sha256", "w") as f:
        f.write(sha(path))
    os.makedirs(os.path.join(BASE, "state"), exist_ok=True)
    os.chmod(os.path.join(BASE, "state"), 0o777)
    log_event("baseline.jsonl", {"stable_pass": len(stable), "flaky": len(flaky)})
    print("baseline: %d stable passing tests, %d flaky" % (len(stable), len(flaky)))


def failure_reasons(out):
    reasons = {}
    for m in re.finditer(r"^(?:FAILED|ERROR) (\S+) - (.*)$", out, re.M):
        reasons[m.group(1)] = m.group(2)[:200]
    return reasons


def check():
    sys.stdin.read()                         # hook 输入（本门禁不需要）
    spec = load_spec()
    max_blocks = int(spec.get("max_blocks", 5))
    counter = os.path.join(BASE, "state", "blocks")
    blocks = int(open(counter).read()) if os.path.exists(counter) else 0
    path = os.path.join(BASE, "baseline.json")
    if not os.path.exists(path):
        log_event("checks.jsonl", {"decision": "allow", "why": "no baseline"})
        return
    tampered = open(path + ".sha256").read().strip() != sha(path)   # agent 若改动基线，记录下来（不能阻止 root）
    if blocks >= max_blocks:
        log_event("checks.jsonl", {"decision": "allow", "why": "block limit reached", "blocks": blocks,
                                   "baseline_tampered": tampered})
        return
    stable = json.load(open(path))["stable_pass"]
    if not stable:
        log_event("checks.jsonl", {"decision": "allow", "why": "empty baseline", "baseline_tampered": tampered})
        return
    t0 = time.time()
    sm, err, _, out = run_tests(spec)
    if sm is None:
        log_event("checks.jsonl", {"decision": "allow", "why": err, "sec": round(time.time() - t0)})
        return
    regressions = [t for t in stable if sm.get(t) not in OK]
    rec = {"blocks_before": blocks, "n_regressions": len(regressions), "regressions": regressions[:50],
           "sec": round(time.time() - t0), "baseline_tampered": tampered}
    if not regressions:
        log_event("checks.jsonl", dict(rec, decision="allow", why="no regressions"))
        return
    blocks += 1
    with open(counter, "w") as f:
        f.write(str(blocks))
    reasons = failure_reasons(out)
    lines = ["- %s%s" % (t, (": " + reasons[t]) if t in reasons else ("" if t in sm else ": not run / not found"))
             for t in regressions[:30]]
    more = "\n... and %d more" % (len(regressions) - 30) if len(regressions) > 30 else ""
    reason = ("Regression check failed (%d of %d). These %d tests passed on the original code before your changes, "
              "and they do not pass now:\n%s%s\n\nFix these regressions before finishing."
              % (blocks, max_blocks, len(regressions), "\n".join(lines), more))
    log_event("checks.jsonl", dict(rec, decision="block"))
    print(json.dumps({"decision": "block", "reason": reason}))


if __name__ == "__main__":
    {"baseline": baseline, "check": check}[sys.argv[1]]()
