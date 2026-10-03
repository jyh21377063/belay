"""bash 命令的粗分类：测试命令、只读命令、普通运行命令。

只在引号外的 &&、||、;、|、&、换行处切段，只看每一段的命令词（去掉 VAR=x 前缀和 timeout / nice / env / uv run /
python -m 之类的包装），不对整条命令做子串搜索：cat pytest.ini、grep -rn "pytest\\|unittest"、pip install pytest、
ps aux | grep pytest 都不是测试。heredoc 的正文不当命令看。解析失败（引号不配对）时保守：不算测试、不算运行、不算只读。
"""
from __future__ import annotations

import re
import shlex
from typing import Optional

TEST = "test"
RUN = "run"
READ = "read"
SETUP = "setup"
NEUTRAL = "neutral"

_PUNCT = "();<>|&\n"
_HEREDOC = re.compile(r"(?<!<)<<-?(?!<)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_DURATION = re.compile(r"^\d+(\.\d+)?[smhd]?$")
_PYTHON = re.compile(r"^python(\d+(\.\d+)?)?$")
_TEST_SCRIPT = re.compile(r"^(run_?)?tests?(\.(py|sh))?$|^runtests(\.(py|sh))?$")

WRAPPERS = {"time", "nohup", "nice", "ionice", "timeout", "env", "sudo", "exec", "command", "xvfb-run", "stdbuf",
            "taskset", "chronic", "unbuffer"}
RUNNERS = {"uv", "poetry", "pipenv", "pdm", "hatch", "rye"}                # 后面跟 run 的包装
TEST_TOOLS = {"pytest", "py.test", "nosetests", "nose2", "tox", "nox", "jest", "mocha", "vitest", "ctest", "phpunit",
              "rspec", "trial", "unittest", "ava", "karma"}
READ_ONLY = {"ls", "cat", "head", "tail", "grep", "egrep", "fgrep", "rg", "ag", "find", "fd", "wc", "echo", "pwd",
             "tree", "file", "stat", "less", "more", "diff", "cmp", "sort", "uniq", "awk", "cut", "tr", "column", "jq",
             "which", "whereis", "type", "printf", "du", "df", "realpath", "readlink", "dirname", "basename", "date",
             "nl", "od", "xxd", "hexdump", "strings", "md5sum", "sha1sum", "sha256sum", "test", "[", "ps", "pgrep",
             "jobs", "top", "htop", "free", "uptime", "id", "whoami", "hostname", "uname", "nproc", "lsof", "lscpu",
             "printenv", "locale", "tac", "rev", "fold", "fmt", "paste", "comm", "join", "seq", "zcat"}
NEUTRAL_CMDS = {"cd", "pushd", "popd", "true", ":", "false", "sleep", "wait", "clear", "history", "set", "unset",
                "export", "alias", "source", ".", "shopt", "ulimit", "umask", "trap", "hash"}
FILE_OPS = {"mkdir", "cp", "mv", "rm", "rmdir", "touch", "chmod", "chown", "chgrp", "ln", "tee", "install", "kill",
            "pkill", "killall", "tar", "unzip", "gunzip", "gzip", "zip", "bunzip2", "curl", "wget", "patch", "rsync",
            "apt", "apt-get", "dpkg", "yum", "dnf", "apk", "conda", "mamba", "micromamba", "brew", "pipx", "pip",
            "pip3", "gem", "truncate", "dd", "shred"}
GIT_READ = {"diff", "log", "status", "show", "blame", "ls-files", "ls-tree", "rev-parse", "branch", "remote",
            "describe", "grep", "shortlog", "cat-file", "reflog", "config"}
PKG_SETUP = {"install", "i", "ci", "add", "remove", "rm", "uninstall", "link", "init", "sync", "lock", "update",
             "upgrade", "get", "fetch", "mod", "venv", "pip", "tidy", "outdated", "list", "info", "show", "audit"}


def _strip_heredocs(cmd: str) -> str:
    """去掉续行与 heredoc 的正文（正文不是命令）。"""
    out: list[str] = []
    ends: list[tuple[str, bool]] = []
    for line in cmd.replace("\\\n", " ").split("\n"):
        if ends:
            word, tabs = ends[0]
            if (line.lstrip("\t") if tabs else line).rstrip() == word:
                ends.pop(0)
            continue
        out.append(line)
        ends = [(m.group(2), m.group(0).startswith("<<-")) for m in _HEREDOC.finditer(line)]
    return "\n".join(out)


def segments(cmd: str) -> Optional[list[list[str]]]:
    """按引号外的分隔符切成段，每段是去掉重定向之后的词；解析失败返回 None。"""
    lex = shlex.shlex(_strip_heredocs(cmd), posix=True, punctuation_chars=_PUNCT)
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    try:
        tokens = list(lex)
    except ValueError:
        return None
    segs: list[list[str]] = [[]]
    skip = False
    for tok in tokens:
        if skip:
            skip = False
            continue
        if tok and all(c in _PUNCT for c in tok):
            if "<" in tok or ">" in tok:
                skip = True                                 # 重定向：下一个词是目标文件 / 描述符
                continue
            if segs[-1]:
                segs.append([])
            continue
        segs[-1].append(tok)
    return [s for s in segs if s]


def _base(word: str) -> str:
    return word.rsplit("/", 1)[-1]


def head(words: list[str]) -> tuple[str, list[str]]:
    """一段的命令词与参数：去掉 VAR=x 前缀、包装器（timeout 600、nice -n 10、env、uv run ……）、python -m X → X。"""
    i = 0
    while i < len(words):
        w = words[i]
        b = _base(w)
        if _ASSIGN.match(w):
            i += 1
            continue
        if b in RUNNERS and i + 1 < len(words) and words[i + 1] == "run":
            i += 2
            continue
        if b in WRAPPERS:
            i += 1
            while i < len(words) and (words[i].startswith("-") or _ASSIGN.match(words[i])
                                      or _DURATION.match(words[i])):
                i += 1
            continue
        if _PYTHON.match(b) and i + 2 < len(words) and words[i + 1] == "-m":
            return _base(words[i + 2]), words[i + 3:]
        return b, words[i + 1:]
    return "", []


def classify_segment(words: list[str]) -> str:
    cmd, args = head(words)
    first = args[0] if args else ""
    if not cmd or cmd in NEUTRAL_CMDS:
        return NEUTRAL
    if cmd in TEST_TOOLS or _TEST_SCRIPT.match(cmd):
        return TEST
    if cmd in ("cargo", "go", "dotnet", "mix", "bazel", "bazelisk", "swift", "stack", "cabal", "deno", "zig") \
            and first == "test":
        return TEST
    if cmd in ("npm", "yarn", "pnpm", "bun") and (first in ("test", "t") or
                                                   (first == "run" and len(args) > 1 and args[1].startswith("test"))):
        return TEST
    if cmd in ("mvn", "mvnw", "gradle", "gradlew") and any(a in ("test", "check", "verify") or a.endswith(":test")
                                                           for a in args):
        return TEST
    if cmd == "make" and any(a in ("test", "tests", "check") for a in args if not a.startswith("-")):
        return TEST
    if cmd in READ_ONLY:
        return READ
    if cmd == "sed":
        return SETUP if any(a.startswith("-i") or a.startswith("--in-place") for a in args) else READ
    if cmd == "git":
        sub = next((a for a in args if not a.startswith("-")), "")
        return READ if sub in GIT_READ else SETUP
    if cmd == "xargs":
        rest = list(args)
        while rest and rest[0].startswith("-"):
            rest = rest[1:]
        return classify_segment(rest) if rest else READ
    if cmd in FILE_OPS:
        return SETUP
    if cmd in ("npm", "yarn", "pnpm", "bun", "cargo", "go", "uv", "poetry", "pipenv", "pdm", "rye", "hatch") \
            and first in PKG_SETUP:
        return SETUP
    return RUN


def kinds(cmd: str) -> Optional[list[str]]:
    segs = segments(cmd)
    return None if segs is None else [classify_segment(s) for s in segs]


def is_test_command(cmd: str) -> bool:
    """至少一段的命令词是测试工具（pytest、python -m pytest、make test、npm test、cargo test ……）。"""
    k = kinds(cmd)
    return bool(k) and TEST in k


def is_read_only(cmd: str) -> bool:
    """每一段都只是读取 / 查看（cd 之类不算一段）。"""
    k = kinds(cmd)
    return k is not None and all(x in (READ, NEUTRAL) for x in k)


def is_run_command(cmd: str, generic: bool = True) -> bool:
    """跑了测试，或（generic 时）跑了普通程序：不全是只读、安装、搬文件。"""
    k = kinds(cmd)
    return bool(k) and (TEST in k or (generic and RUN in k))


_FAILED = re.compile(r"\[(exit code -?\d+(, no output)?|Command timed out after \d+s[^\]]*)\]\s*$")


def bash_passed(inp: dict, output: str) -> bool:
    """bash 工具的结果是否表示命令在前台跑完且退出码为 0（见 tools/shell.py 的输出格式）。"""
    if inp.get("run_in_background"):
        return False
    return not _FAILED.search(str(output or "")[-400:])
