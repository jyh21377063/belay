"""容器内的检查运行器：只用标准库，兼容 Python 3.6；不 import belay 的其他模块（由宿主机上传执行）。

  python3 runner.py pytest SPEC OUT_DIR       跑一次测试，写 OUT_DIR/result.json 与 OUT_DIR/log
  python3 runner.py strip-tests GIT_DIR BASE_TREE TREE   把 TREE 中测试路径下的改动恢复为 BASE_TREE 的版本，
                                                         输出 {"tree": 新树, "dropped": [路径]}
  python3 runner.py probe SPEC                 在工作区的测试环境里打印 sys.path（检查 import 到的是哪份代码）

SPEC（JSON）：
  workspace    运行测试的目录
  prelude      运行前的 shell 语句（如激活 conda 环境）
  commands     测试前执行的命令列表
  test_cmd     测试命令；其中的路径参数（含 / 或以 .py 结尾）可被 select 替换，不存在的路径会被去掉
  parser       parse_log_pytest | parse_log_pytest_pydantic
  select       测试文件或 node id 列表；空 = 用 test_cmd 原有的路径（全量）
  extra        追加的测试目标（不替换原有路径），例如放进工作区的独立测试
  overlay      {工作区相对路径: 源文件}：运行期间临时放入工作区（独立测试），结束后删除
  tree         可选：运行期间把工作区临时切换为这个树（只改动不同的文件），结束后恢复
  git_dir / index   tree 需要的影子仓库与临时索引文件
  pythonpath   是否把工作区放到 PYTHONPATH 最前面（在原始代码副本上验证测试时用）
  timeout      秒
  chown        结束后把工作区改回给这个用户（runtime 以 root 运行门禁时用）

测试路径的规则与 belay/graph/evidence.py 的 TEST_PATH 保持一致；解析器移植自 eval/agents/gate_script.py
（SWE-EVO 官方 SWE-bench 分支的 parse_log_pytest / parse_log_pytest_pydantic）。
"""
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time

TEST_PATH = re.compile(r"(^|/)(tests?|testing|__tests__)/|(^|/)test_[^/]*\.py$|_test\.py$|(^|/)conftest\.py$")
STATUSES = ("FAILED", "PASSED", "SKIPPED", "ERROR", "XFAIL")
GIT = ["git", "-c", "safe.directory=*", "-c", "core.quotepath=off"]


# ---- 解析 -------------------------------------------------------------------------

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


def failure_reasons(log):
    """-rA 的简短汇总：'FAILED id - reason' / 'ERROR id - reason'。"""
    reasons = {}
    for m in re.finditer(r"^(?:FAILED|ERROR) (\S+) - (.*)$", log, re.M):
        reasons[m.group(1)] = m.group(2)[:400]
    return reasons


# ---- 命令 -------------------------------------------------------------------------

def _looks_like_path(tok):
    return not tok.startswith("-") and ("/" in tok or tok.split("::")[0].endswith(".py"))


def build_command(spec):
    """返回（命令，是否有测试目标，被去掉的参数）。"""
    ws = spec["workspace"]
    toks = shlex.split(spec["test_cmd"])
    select = list(spec.get("select") or [])
    extra = list(spec.get("extra") or [])          # 追加的目标（独立测试），不替换原有路径
    kept, dropped, targets = [], [], []
    for tok in toks:
        if _looks_like_path(tok):
            if select:
                continue                            # 由 select 替换
            (targets if os.path.exists(os.path.join(ws, tok.split("::")[0])) else dropped).append(tok)
        else:
            kept.append(tok)
    for tok in select + [t for t in extra if t not in select]:
        (targets if os.path.exists(os.path.join(ws, tok.split("::")[0])) else dropped).append(tok)
    parts = []
    if spec.get("pythonpath"):
        parts.append("export PYTHONPATH=%s${PYTHONPATH:+:$PYTHONPATH}" % shlex.quote(ws))
    parts += [spec.get("prelude") or "true", "cd " + shlex.quote(ws)] + list(spec.get("commands") or [])
    parts.append(" ".join(shlex.quote(t) for t in kept + targets))
    return "; ".join(parts), bool(targets), dropped


class Terminated(Exception):
    """收到 SIGTERM（作业被取消）：杀掉测试进程组后照常恢复工作区。"""


def _on_term(signum, frame):
    raise Terminated()


def _kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    proc.wait()


def run_shell(cmd, log_path, timeout):
    """在独立进程组中运行，输出写入日志文件；超时杀掉整个进程组。返回（退出码，是否超时）。"""
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(["bash", "-c", cmd], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return proc.wait(timeout=timeout), False
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            return -9, True
        except Terminated:
            _kill_group(proc)
            raise


# ---- git ---------------------------------------------------------------------------

def git(git_dir, args, work_tree=None, index=None, stdin=None, check=True):
    env = dict(os.environ)
    if index:
        env["GIT_INDEX_FILE"] = index
    cmd = GIT + ["--git-dir=" + git_dir] + (["--work-tree=" + work_tree] if work_tree else []) + args
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    out, err = p.communicate(stdin)
    if check and p.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args[:3]), err.decode("utf-8", "replace")[:500]))
    return out


def diff_tree(git_dir, a, b):
    """[(status, path, old_mode, old_sha, new_mode, new_sha)]"""
    out = git(git_dir, ["diff-tree", "-r", "--no-renames", "-z", a, b])
    items = out.split(b"\0")
    res = []
    i = 0
    while i < len(items) - 1:
        meta = items[i].decode()
        path = items[i + 1].decode("utf-8", "surrogateescape")
        i += 2
        if not meta.startswith(":"):
            continue
        m1, m2, s1, s2, st = meta[1:].split()
        res.append((st[0], path, m1, s1, m2, s2))
    return res


def strip_tests(git_dir, base, tree, tmp_index):
    changes = diff_tree(git_dir, base, tree)
    lines, dropped = [], []
    for st, path, m1, s1, _m2, _s2 in changes:
        if not TEST_PATH.search(path):
            continue
        dropped.append(path)
        if st == "A":
            lines.append("0 %s\t%s" % ("0" * 40, path))
        else:
            lines.append("%s %s\t%s" % (m1, s1, path))
    if not dropped:
        return tree, []
    git(git_dir, ["read-tree", tree], index=tmp_index)
    git(git_dir, ["update-index", "--index-info"], index=tmp_index,
        stdin=("\n".join(lines) + "\n").encode("utf-8", "surrogateescape"))
    new = git(git_dir, ["write-tree"], index=tmp_index).decode().strip()
    os.remove(tmp_index)
    return new, dropped


class TreeOverlay(object):
    """运行期间把工作区临时切换为指定的树：只改动与当前内容不同的文件，结束后原样恢复。"""

    def __init__(self, spec, out_dir):
        self.spec = spec
        self.ws = spec["workspace"]
        self.saved_dir = os.path.join(out_dir, "saved")
        self.touched = []           # (相对路径, 原来是否存在)
        self.index = spec.get("index") or os.path.join(out_dir, "index")

    def __enter__(self):
        tree = self.spec.get("tree")
        if not tree:
            return self
        gd = self.spec["git_dir"]
        if os.path.exists(self.index):
            os.remove(self.index)
        git(gd, ["add", "-A", "."], work_tree=self.ws, index=self.index)
        current = git(gd, ["write-tree"], index=self.index).decode().strip()
        changes = diff_tree(gd, current, tree)
        present = []
        for st, path, _m1, _s1, _m2, _s2 in changes:
            full = os.path.join(self.ws, path)
            existed = os.path.lexists(full)
            if existed:
                dst = os.path.join(self.saved_dir, path)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(full, dst, follow_symlinks=False)
            self.touched.append((path, existed))
            if st == "D":
                os.remove(full)
            else:
                present.append(path)
        if present:
            git(gd, ["read-tree", tree], index=self.index)
            git(gd, ["checkout-index", "-f", "-z", "--stdin"], work_tree=self.ws, index=self.index,
                stdin=("\0".join(present) + "\0").encode("utf-8", "surrogateescape"))
        return self

    def __exit__(self, *exc):
        for path, existed in self.touched:
            full = os.path.join(self.ws, path)
            if os.path.lexists(full):
                os.remove(full)
            if existed:
                os.makedirs(os.path.dirname(full), exist_ok=True)
                shutil.copy2(os.path.join(self.saved_dir, path), full, follow_symlinks=False)
        if os.path.exists(self.index):
            os.remove(self.index)
        return False


class FileOverlay(object):
    def __init__(self, spec):
        self.ws = spec["workspace"]
        self.files = spec.get("overlay") or {}
        self.placed = []

    def __enter__(self):
        for rel, src in self.files.items():
            dst = os.path.join(self.ws, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            self.placed.append(dst)
        return self

    def __exit__(self, *exc):
        for dst in self.placed:
            if os.path.exists(dst):
                os.remove(dst)
            d = os.path.dirname(dst)
            if os.path.isdir(d) and not os.listdir(d) and os.path.basename(d) == ".belay_checks":
                os.rmdir(d)
        return False


# ---- 子命令 -----------------------------------------------------------------------

def cmd_pytest(spec_path, out_dir):
    signal.signal(signal.SIGTERM, _on_term)
    with open(spec_path) as f:
        spec = json.load(f)
    os.makedirs(out_dir, exist_ok=True)
    log_path = os.path.join(out_dir, "log")
    result = {"status": "ok", "tests": {}, "reasons": {}, "rc": None, "sec": 0, "dropped": [], "error": ""}
    t0 = time.time()
    try:
        with TreeOverlay(spec, out_dir), FileOverlay(spec):
            cmd, has_target, dropped = build_command(spec)
            result["dropped"] = dropped
            if not has_target:
                result.update(status="error", error="no test files exist for this selection: %s" % dropped[:10])
            else:
                rc, timed_out = run_shell(cmd, log_path, int(spec.get("timeout") or 3600))
                result["rc"] = rc
                with open(log_path, "rb") as f:
                    log = f.read().decode("utf-8", "replace")
                result["tests"] = PARSERS[spec.get("parser") or "parse_log_pytest"](log)
                result["reasons"] = failure_reasons(log)
                if timed_out:
                    result.update(status="timeout", error="timed out after %ss" % spec.get("timeout"))
                elif not result["tests"]:
                    tail = log[-1500:]
                    result.update(status="error", error="no test results in the output; tail:\n" + tail)
    except Terminated:
        result.update(status="error", error="cancelled")
    except Exception as e:                          # 记录到结果里，不让作业悄无声息地失败
        result.update(status="error", error="%s: %s" % (type(e).__name__, e))
    finally:
        if spec.get("chown"):
            subprocess.call(["chown", "-R", "%s:%s" % (spec["chown"], spec["chown"]), spec["workspace"]])
    result["sec"] = round(time.time() - t0, 1)
    tmp = os.path.join(out_dir, "result.json.tmp")
    with open(tmp, "w") as f:
        json.dump(result, f)
    os.rename(tmp, os.path.join(out_dir, "result.json"))


def cmd_strip_tests(git_dir, base, tree):
    tmp_index = os.path.join(git_dir, "strip-index-%d" % os.getpid())
    new, dropped = strip_tests(git_dir, base, tree, tmp_index)
    print(json.dumps({"tree": new, "dropped": dropped}))


def cmd_probe(spec_path):
    with open(spec_path) as f:
        spec = json.load(f)
    parts = []
    if spec.get("pythonpath"):
        parts.append("export PYTHONPATH=%s${PYTHONPATH:+:$PYTHONPATH}" % shlex.quote(spec["workspace"]))
    parts += [spec.get("prelude") or "true", "cd " + shlex.quote(spec["workspace"])] + list(spec.get("commands") or [])
    parts.append("python -c 'import json, sys; print(\"SYS_PATH=\" + json.dumps(sys.path))'")
    out = subprocess.Popen(["bash", "-c", "; ".join(parts)], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT).communicate()[0].decode("utf-8", "replace")
    m = re.search(r"^SYS_PATH=(.*)$", out, re.M)
    print(json.dumps({"sys_path": json.loads(m.group(1)) if m else None, "output": out[-1000:]}))


def main(argv):
    if len(argv) < 2:
        sys.exit(__doc__)
    cmd = argv[1]
    if cmd == "pytest":
        cmd_pytest(argv[2], argv[3])
    elif cmd == "strip-tests":
        cmd_strip_tests(argv[2], argv[3], argv[4])
    elif cmd == "probe":
        cmd_probe(argv[2])
    else:
        sys.exit("unknown command: " + cmd)


if __name__ == "__main__":
    main(sys.argv)
