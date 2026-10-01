"""容器内的检查运行器：只用标准库，兼容 Python 3.6；不 import belay 的其他模块（由宿主机上传执行）。

  python3 runner.py run SPEC OUT_DIR          跑一次检查（pytest 与命令检查），写 OUT_DIR/result.json 与 OUT_DIR/log
                                               （pytest 是 run 的别名）
  python3 runner.py strip-tests GIT_DIR BASE_TREE TREE   把 TREE 中测试路径下的改动恢复为 BASE_TREE 的版本，
                                                         输出 {"tree": 新树, "dropped": [路径]}
  python3 runner.py probe SPEC                 在测试环境里打印 sys.path（基线阶段用来把工作区路径映射到验证槽位）
  python3 runner.py isolation-probe SPEC OUT   在验证槽位里检查导入隔离：import 解析到槽位；破坏一个被测试导入的源文件，
                                               那个测试必须失败。输出 {"ok": ..., "reason": ..., "inconclusive": ...}

SPEC（JSON）：
  workspace    运行测试的目录
  prelude      运行前的 shell 语句（如激活 conda 环境）
  commands     测试前执行的命令列表
  test_cmd     测试命令；其中的路径参数（含 / 或以 .py 结尾）可被 select 替换，不存在的路径会被去掉
  parser       parse_log_pytest | parse_log_pytest_pydantic
  select       测试文件或 node id 列表；空 = 用 test_cmd 原有的路径（全量）
  extra        追加的测试目标（不替换原有路径），例如放进工作区的独立测试
  overlay      {工作区相对路径: 源文件}：运行期间临时放入工作区（独立测试），结束后删除
  tree         可选：运行期间把工作区临时切换为这个树（只改动不同的文件），结束后恢复（降级模式、基线的工作区那一次）
  git_dir / index   tree 需要的影子仓库与临时索引文件
  slot         可选：在验证槽位里运行（模块 A）。先把 tree 增量导出到槽位（read-tree --reset -u 只改动不同的文件，
               被忽略的构建产物留作缓存，未被忽略的未跟踪文件清掉）；第一次导出后从工作区复制被忽略的文件作为
               构建缓存种子（cp -a --reflink=auto）。workspace 此时就是槽位目录。
  slot_index / seed_from / seed_index / seed_marker   槽位的索引文件、种子来源、列出工作区被忽略文件用的索引、种子标记
  pythonpath_entries   按原顺序放在 PYTHONPATH 最前的路径（工作区 sys.path 映射到槽位后的结果）
  nice / env   后台作业的 nice 值与额外环境变量（限制并发）
  pythonpath   是否把工作区放到 PYTHONPATH 最前面（在原始代码副本上验证测试时用）
  timeout      秒
  chown        结束后把工作区改回给这个用户（runtime 以 root 运行门禁时用）
  skip_tests   不跑测试命令（选择里只有命令检查时）
  checks       命令检查 [{"id": "cmd:build", "command": "..."}]：在同一个候选树上执行，退出码 0 记为 PASSED

测试路径的规则与 belay/core/verify.py 的 TEST_PATH 保持一致；解析器移植自 eval/agents/gate_script.py
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

_ARG_OPTS = ("-m", "-p", "-k", "-c", "-o", "--rootdir", "--confcutdir", "-W")


def _looks_like_path(tok, ws=None, prev=None):
    if tok.startswith("-") or prev in _ARG_OPTS:
        return False
    if "/" in tok or "::" in tok or tok.split("::")[0].endswith(".py"):
        return True
    return bool(ws) and os.path.isdir(os.path.join(ws, tok))


def build_command(spec):
    """返回（命令，是否有测试目标，被去掉的参数）。

    test_cmd 里的路径参数（含 /、::、以 .py 结尾，或工作区里存在的目录）在 select 非空时被替换；
    test_cmd 本身不带路径参数时（例如 `python -m pytest`），全量运行就原样执行。
    """
    ws = spec["workspace"]
    toks = shlex.split(spec["test_cmd"])
    select = list(spec.get("select") or [])
    extra = list(spec.get("extra") or [])          # 追加的目标（独立测试），不替换原有路径
    kept, dropped, targets = [], [], []
    had_paths = False
    prev = None
    for tok in toks:
        if _looks_like_path(tok, ws, prev):
            had_paths = True
            if not select:
                (targets if os.path.exists(os.path.join(ws, tok.split("::")[0])) else dropped).append(tok)
        else:
            kept.append(tok)
        prev = tok
    for tok in select + [t for t in extra if t not in select]:
        (targets if os.path.exists(os.path.join(ws, tok.split("::")[0])) else dropped).append(tok)
    parts = env_prefix(spec)
    parts += [spec.get("prelude") or "true", "cd " + shlex.quote(ws)] + list(spec.get("commands") or [])
    parts.append(" ".join(shlex.quote(t) for t in kept + targets))
    has_target = bool(targets) or (not select and not had_paths)
    return "; ".join(parts), has_target, dropped


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
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                         cwd=work_tree or None)
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


def export_slot(spec):
    """把 tree 增量导出到验证槽位：只改动与槽位索引不同的文件；被忽略的文件（构建缓存）保留。"""
    gd, slot, idx, tree = spec["git_dir"], spec["slot"], spec["slot_index"], spec["tree"]
    if not os.path.isdir(slot):
        os.makedirs(slot)
    git(gd, ["read-tree", "--reset", "-u", tree], work_tree=slot, index=idx)
    git(gd, ["clean", "-f", "-d", "-q"], work_tree=slot, index=idx)
    marker = spec.get("seed_marker")
    if spec.get("seed_from") and marker and not os.path.exists(marker):
        seed_slot(spec)
        with open(marker, "w") as f:
            f.write(tree)


def seed_slot(spec):
    """构建缓存种子：工作区里被忽略的文件与目录（target/、build/、node_modules/、大文件……）以及项目自己的 .git。"""
    ws, slot, gd = spec["seed_from"], spec["slot"], spec["git_dir"]
    idx = spec.get("seed_index")
    tmp_idx = None
    if not idx or not os.path.exists(idx):
        tmp_idx = os.path.join(os.path.dirname(spec["slot_index"]), "seed-index-%d" % os.getpid())
        git(gd, ["read-tree", spec["tree"]], index=tmp_idx)
        idx = tmp_idx
    out = git(gd, ["ls-files", "-z", "-o", "-i", "--exclude-standard", "--directory"], work_tree=ws, index=idx,
              check=False)
    if tmp_idx and os.path.exists(tmp_idx):
        os.remove(tmp_idx)
    paths = [p.decode("utf-8", "surrogateescape").rstrip("/") for p in out.split(b"\0") if p]
    for i in range(0, len(paths), 200):
        chunk = [p for p in paths[i:i + 200] if p and not p.startswith("../")]
        if chunk:
            subprocess.call(["cp", "-a", "--reflink=auto", "--parents"] + chunk + [slot + "/"], cwd=ws,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    seed_git(ws, slot)


def seed_git(ws, slot):
    """有些测试会调用 git（setuptools_scm、版本号、git describe）。不整份复制项目的 .git（大仓库会多占几个 GB），
    而是在槽位里建一个轻量仓库：对象经 alternates 借用工作区的 .git/objects（只读），复制 HEAD 与引用，
    索引按 HEAD 生成。槽位里的 git 写操作只落在这个轻量仓库里，碰不到工作区的仓库。"""
    src = os.path.join(ws, ".git")
    dst = os.path.join(slot, ".git")
    if not os.path.isdir(src) or os.path.exists(dst):
        return
    try:
        subprocess.call(GIT + ["init", "-q", "--template=", slot], stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)
        info = os.path.join(dst, "objects", "info")
        if not os.path.isdir(info):
            os.makedirs(info)
        with open(os.path.join(info, "alternates"), "w") as f:
            f.write(os.path.realpath(os.path.join(src, "objects")) + "\n")
        for name in ("HEAD", "packed-refs", "shallow"):
            if os.path.isfile(os.path.join(src, name)):
                shutil.copy2(os.path.join(src, name), os.path.join(dst, name))
        if os.path.isdir(os.path.join(src, "refs")):
            shutil.rmtree(os.path.join(dst, "refs"), ignore_errors=True)
            shutil.copytree(os.path.join(src, "refs"), os.path.join(dst, "refs"), symlinks=True)
        subprocess.call(GIT + ["-C", slot, "reset", "-q"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        shutil.rmtree(dst, ignore_errors=True)          # 没建成就不要留下半个仓库


def env_prefix(spec):
    parts = []
    entries = [e for e in (spec.get("pythonpath_entries") or []) if e]
    if entries:
        parts.append("export PYTHONPATH=%s${PYTHONPATH:+:$PYTHONPATH}" % shlex.quote(":".join(entries)))
    if spec.get("pythonpath"):
        parts.append("export PYTHONPATH=%s${PYTHONPATH:+:$PYTHONPATH}" % shlex.quote(spec["workspace"]))
    for k, v in sorted((spec.get("env") or {}).items()):
        parts.append("export %s=%s" % (k, shlex.quote(str(v))))
    return parts


def niced(cmd, spec):
    n = int(spec.get("nice") or 0)
    return "nice -n %d bash -c %s" % (n, shlex.quote(cmd)) if n > 0 else cmd


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
        if spec.get("slot"):
            export_slot(spec)
            spec = dict(spec, workspace=spec["slot"], tree=None)
        with TreeOverlay(spec, out_dir), FileOverlay(spec):
            if spec.get("test_cmd") and not spec.get("skip_tests"):
                cmd, has_target, dropped = build_command(spec)
                result["dropped"] = dropped
                if not has_target:
                    result.update(status="error", error="no test files exist for this selection: %s" % dropped[:10])
                else:
                    rc, timed_out = run_shell(niced(cmd, spec), log_path, int(spec.get("timeout") or 3600))
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
            for chk in spec.get("checks") or []:
                clog = os.path.join(out_dir, "check-%s.log" % re.sub(r"[^A-Za-z0-9_.-]", "_", chk["id"]))
                parts = env_prefix(spec) + [spec.get("prelude") or "true", "cd " + shlex.quote(spec["workspace"]),
                                            chk["command"]]
                rc, timed_out = run_shell(niced("; ".join(parts), spec), clog,
                                          int(chk.get("timeout") or spec.get("timeout") or 3600))
                result["tests"][chk["id"]] = "PASSED" if rc == 0 and not timed_out else "FAILED"
                if rc != 0:
                    with open(clog, "rb") as f:
                        result["reasons"][chk["id"]] = f.read().decode("utf-8", "replace")[-400:]
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


def _sys_path(spec, module=None):
    parts = env_prefix(spec) + [spec.get("prelude") or "true", "cd " + shlex.quote(spec["workspace"])]
    parts += list(spec.get("commands") or [])
    code = "import json, sys; print('SYS_PATH=' + json.dumps(sys.path))"
    if module:
        code = ("import importlib, json, sys; m = importlib.import_module(%r); "
                "print('MODULE_FILE=' + json.dumps(getattr(m, '__file__', None) or ''))" % module)
    parts.append("python -c %s" % shlex.quote(code))
    out = subprocess.Popen(["bash", "-c", "; ".join(parts)], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT).communicate()[0].decode("utf-8", "replace")
    return out


def cmd_probe(spec_path):
    with open(spec_path) as f:
        spec = json.load(f)
    out = _sys_path(spec)
    m = re.search(r"^SYS_PATH=(.*)$", out, re.M)
    print(json.dumps({"sys_path": json.loads(m.group(1)) if m else None, "output": out[-1000:]}))


def _imported_modules(path):
    import ast
    try:
        with open(path, "rb") as f:
            tree = ast.parse(f.read())
    except Exception:
        return []
    mods = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            mods += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            mods.append(node.module)
            mods += ["%s.%s" % (node.module, a.name) for a in node.names]
    return mods


def _stdlib(mod):
    """标准库模块（3.10 起有 sys.stdlib_module_names；更早的版本只能认出内建模块）。"""
    top = mod.split(".")[0]
    names = set(getattr(sys, "stdlib_module_names", ())) | set(sys.builtin_module_names)
    return top in names


def _module_file(roots, mod, slot=None):
    """模块对应的源文件：先在映射的路径里找，再在槽位里按路径后缀找（发现没被映射的源码目录）。

    按后缀找时，找到的文件必须真能以这个名字被导入：它所在的目录（顶层模块的父目录）本身不是一个包
    （没有 __init__.py）。否则 `import typing` 会被当成 pydantic/typing.py、`import utils` 会被当成
    dask/utils.py，探针在槽位里导入时解析到标准库或别的包，把有效的隔离误判为无效（整个运行退化为降级模式）。"""
    rel = mod.replace(".", "/")
    for root in roots:
        for cand in (rel + ".py", rel + "/__init__.py"):
            full = os.path.join(root, cand)
            if os.path.isfile(full):
                return full
    if slot and not _stdlib(mod):
        skip = {".git", "node_modules", "__pycache__", ".tox", ".venv", "venv", "build", "dist"}
        for dirpath, dirnames, filenames in os.walk(slot):
            depth = dirpath[len(slot):].count(os.sep)
            dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")] if depth < 4 else []
            if dirpath != slot and "__init__.py" in filenames:
                continue                                # 包里面的文件不是顶层模块
            for cand in (rel + ".py", rel + "/__init__.py"):
                full = os.path.join(dirpath, cand)
                if os.path.isfile(full):
                    return full
    return None


def cmd_isolation_probe(spec_path, out_dir):
    """导入隔离探针：在槽位里（1）确认被测试导入的模块解析到槽位；（2）破坏它，那个测试必须失败。"""
    with open(spec_path) as f:
        spec = json.load(f)
    os.makedirs(out_dir, exist_ok=True)
    if spec.get("slot") and spec.get("tree"):
        export_slot(spec)
    slot = spec["workspace"]
    roots = [e for e in (spec.get("pythonpath_entries") or []) if e.startswith(slot)] + [slot]
    result = {"ok": True, "inconclusive": True, "reason": "no test imports a source module in the verification "
              "directory; nothing to check"}
    tried = 0
    for test_file in spec.get("candidates") or []:
        full_test = os.path.join(slot, test_file)
        if not os.path.isfile(full_test):
            continue
        for mod in _imported_modules(full_test):
            src = _module_file(roots, mod, slot)
            if not src or TEST_PATH.search(os.path.relpath(src, slot)):
                continue
            tried += 1
            out = _sys_path(spec, mod)
            m = re.search(r"^MODULE_FILE=(.*)$", out, re.M)
            if not m:
                if tried >= 5:
                    break
                continue                                # 导入本身失败：换一个模块
            where = os.path.realpath(json.loads(m.group(1)) or "")
            if not where.startswith(os.path.realpath(slot) + os.sep):
                result = {"ok": False, "inconclusive": False, "module": mod,
                          "reason": "importing %s in the verification directory resolves to %s" % (mod, where)}
                print(json.dumps(result))
                return
            with open(src, "rb") as f:
                original = f.read()
            try:
                with open(src, "wb") as f:
                    f.write(b"raise ImportError('belay isolation probe')\n" + original)
                cmd, has_target, _ = build_command(dict(spec, select=[test_file]))
                log = os.path.join(out_dir, "probe.log")
                rc, _ = run_shell(cmd, log, int(spec.get("timeout") or 600))
                with open(log, "rb") as f:
                    text = f.read().decode("utf-8", "replace")
                passed = [t for t, st in PARSERS[spec.get("parser") or "parse_log_pytest"](text).items()
                          if st == "PASSED"]
            finally:
                with open(src, "wb") as f:
                    f.write(original)
            if passed:
                result = {"ok": False, "inconclusive": False, "module": mod, "test": test_file,
                          "reason": "breaking %s in the verification directory did not break %s (%d passed)"
                                    % (os.path.relpath(src, slot), test_file, len(passed))}
            else:
                result = {"ok": True, "inconclusive": False, "module": mod, "test": test_file,
                          "reason": "imports resolve to the verification directory"}
            print(json.dumps(result))
            return
    print(json.dumps(result))


def main(argv):
    if len(argv) < 2:
        sys.exit(__doc__)
    cmd = argv[1]
    if cmd in ("run", "pytest"):
        cmd_pytest(argv[2], argv[3])
    elif cmd == "strip-tests":
        cmd_strip_tests(argv[2], argv[3], argv[4])
    elif cmd == "probe":
        cmd_probe(argv[2])
    elif cmd == "isolation-probe":
        cmd_isolation_probe(argv[2], argv[3])
    else:
        sys.exit("unknown command: " + cmd)


if __name__ == "__main__":
    main(sys.argv)
