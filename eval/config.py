"""读取 runs.yaml / tasks.yaml，按 defaults ← profile ← 命令行 合并，解析成一个 RunPlan。

这里只做"决定跑什么"，不碰 Docker、不调模型，所以可以放心地单元测试。
"""
from __future__ import annotations

import copy
import datetime as dt
import difflib
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]      # belay/
DEFAULT_RUNS = PROJECT_ROOT / "runs.yaml"
SPLITS = ("test", "dev")
_PLACEHOLDER = re.compile(r"<[^<>]+>")          # 形如 "<DeepSeek 模型名>" 的未填占位符
_ENV_TEMPLATE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class TaskRef:
    benchmark: str
    id: str
    lang: str = ""
    repo: str = ""

    @property
    def key(self) -> str:
        return f"{self.benchmark}/{self.id}"


@dataclass
class Step:
    agent_key: str          # runs.yaml 中 agents 下的名字，如 claude-code
    agent: dict             # 解析后的 agent 定义
    repeats: int
    expect: str | None      # 只有 gold-check 用：pass / fail
    run_id: str

    @property
    def grade_mode(self) -> str:
        return self.agent.get("grade", "replay")


@dataclass
class RunPlan:
    profile: str
    split: str
    tasks: list[TaskRef]
    steps: list[Step]
    concurrency: int
    timeout_min: int
    inline_verify: bool
    keep_containers: bool
    environment: str
    task_dirs: Path
    results_root: Path
    grading_only: bool = False
    source_run: str | None = None
    bench_cfg: dict = None          # tasks.yaml 中的 benchmarks 段

    def task_grading(self, t: TaskRef) -> str:
        """replay：导出补丁、全新容器重放评分；official：按题目自身配置由 Pier 评分（LHTB）。"""
        return ((self.bench_cfg or {}).get(t.benchmark) or {}).get("grading_mode", "replay")

    def solved_threshold(self, t: TaskRef) -> float:
        return float(((self.bench_cfg or {}).get(t.benchmark) or {}).get("solved_threshold", 1.0))

    def task_dir(self, t: TaskRef) -> Path:
        return self.task_dirs / t.benchmark / t.id

    def trial_dir(self, step: Step, t: TaskRef, repeat: int) -> Path:
        return self.results_root / step.run_id / t.benchmark / t.id / str(repeat)

    def source_trial_dir(self, t: TaskRef, repeat: int) -> Path:
        return self.results_root / str(self.source_run) / t.benchmark / t.id / str(repeat)


# ---------------------------------------------------------------- 工具函数
def load_yaml(path: str | Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def resolve_path(base_dir: Path, p: str | Path) -> Path:
    """配置里的相对路径一律相对于该配置文件所在目录解析。"""
    p = Path(p).expanduser()
    return p if p.is_absolute() else (base_dir / p).resolve()


def load_tasks_yaml(path: str | Path) -> dict:
    """读取 tasks.yaml，并把 benchmarks.*.data / eval_scripts 解析为绝对路径。"""
    path = Path(path).resolve()
    if not path.exists():
        raise ConfigError(f"找不到题目清单 {path}（runs.yaml 的 tasks_file）")
    ty = load_yaml(path)
    for bench in (ty.get("benchmarks") or {}).values():
        for key in ("data", "eval_scripts"):
            if bench.get(key):
                bench[key] = str(resolve_path(path.parent, bench[key]))
    return ty


def deep_merge(base: dict, over: dict | None) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def as_list(v) -> list[str] | str:
    """'all' 原样返回；'a,b' 或 ['a','b'] 都转成列表。"""
    if v is None or v == "all":
        return "all"
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return [str(x) for x in v]


def env_templates(agent: dict) -> list[str]:
    """agent.env 中引用的宿主机环境变量名（${VAR}）。"""
    names = []
    for v in (agent.get("env") or {}).values():
        names += _ENV_TEMPLATE.findall(str(v))
    return names


# ---------------------------------------------------------------- 选题
def select_tasks(tasks_yaml: dict, split: str, benchmarks, wanted) -> list[TaskRef]:
    if split not in SPLITS:
        raise ConfigError(f"split 只能是 {SPLITS}，收到 {split!r}")

    known = list(tasks_yaml.get("benchmarks", {}))
    benchmarks = as_list(benchmarks)
    if benchmarks == "all":
        bms = known
    else:
        unknown = [b for b in benchmarks if b not in known]
        if unknown:
            raise ConfigError(f"未知 benchmark {unknown}，可选：{known}")
        bms = benchmarks

    def refs(sp: str, bm_list) -> list[TaskRef]:
        pool = tasks_yaml.get(sp) or {}
        return [TaskRef(bm, e["id"], e.get("lang", ""), e.get("repo", ""))
                for bm in bm_list for e in (pool.get(bm) or [])]

    candidates = refs(split, bms)
    wanted = as_list(wanted)
    if wanted == "all":
        if not candidates:
            raise ConfigError(f"{split} 集在 {bms} 中没有题目")
        return candidates

    whole_split = refs(split, known)                      # 短 id 唯一性在整个 split 内检查
    other_split = refs("dev" if split == "test" else "test", known)
    chosen: list[TaskRef] = []
    for w in wanted:
        if "/" in w:
            bm, tid = w.split("/", 1)
            hits = [t for t in whole_split if t.benchmark == bm and t.id == tid]
        else:
            hits = [t for t in whole_split if t.id == w]
        if not hits:
            msg = f"{split} 集中没有题目 {w!r}"
            if any(w in (t.id, t.key) for t in other_split):
                msg += f"（它在另一个 split 里，是不是忘了 --split {'dev' if split == 'test' else 'test'}？）"
            else:
                near = difflib.get_close_matches(w, [t.id for t in whole_split], n=3)
                if near:
                    msg += f"；相近的：{near}"
            raise ConfigError(msg)
        if len(hits) > 1:
            raise ConfigError(f"短 id {w!r} 在 {split} 中不唯一：{[t.key for t in hits]}，请写完整标识")
        t = hits[0]
        if t.benchmark not in bms:
            raise ConfigError(f"{t.key} 不属于所选 benchmarks {bms}")
        if t not in chosen:
            chosen.append(t)

    order = {t.key: i for i, t in enumerate(candidates)}  # 保持 tasks.yaml 中的顺序
    return sorted(chosen, key=lambda t: order[t.key])


# ---------------------------------------------------------------- agent
def resolve_agent(runs: dict, key: str, model_override: str | None) -> dict:
    agents = runs.get("agents", {})
    if key not in agents:
        raise ConfigError(f"未知 agent {key!r}，可选：{list(agents)}")
    a = copy.deepcopy(agents[key])
    if model_override:
        a["model"] = model_override
    if not a.get("name") and not a.get("import_path"):
        raise ConfigError(f"agent {key} 需要 name（Pier 内置）或 import_path（自定义）")
    fields = [("model", a.get("model"))]
    fields += [(f"env.{k}", v) for k, v in (a.get("env") or {}).items()]
    fields += [(f"kwargs.{k}", v) for k, v in (a.get("kwargs") or {}).items()]
    for field, val in fields:
        if isinstance(val, str) and _PLACEHOLDER.search(val):
            raise ConfigError(f"agent {key} 的 {field} 还是占位符 {val!r}，请在 runs.yaml 填写（kwargs 可直接删掉该项），model 也可用 --model 覆盖")
    return a


# ---------------------------------------------------------------- 入口
def build_plan(runs_path: str | Path, profile: str, overrides: dict | None = None) -> RunPlan:
    overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
    runs_path = Path(runs_path).resolve()
    if not runs_path.exists():
        raise ConfigError(f"找不到运行编排文件 {runs_path}")
    runs = load_yaml(runs_path)
    base = runs_path.parent
    profiles = runs.get("profiles", {})
    if profile not in profiles:
        raise ConfigError(f"未知 profile {profile!r}，可选：{list(profiles)}")

    cfg = deep_merge(runs.get("defaults", {}), profiles[profile])
    cfg = deep_merge(cfg, {k: v for k, v in overrides.items() if k not in ("model", "run_id")})

    split = cfg.get("split")
    tasks_yaml = load_tasks_yaml(resolve_path(base, cfg.get("tasks_file") or runs["tasks_file"]))
    tasks = select_tasks(tasks_yaml, split, cfg.get("benchmarks", "all"), cfg.get("tasks", "all"))

    if "steps" in cfg and "agent" in overrides:
        raise ConfigError(f"profile {profile} 由多个 steps 组成，不能用 --agent 覆盖")
    raw_steps = cfg.get("steps") or [{"agent": cfg.get("agent"), "repeats": cfg.get("repeats", 1)}]
    if cfg.get("grading_only"):
        raw_steps = [{"agent": "replay", "repeats": cfg.get("repeats", 1)}]

    today = dt.date.today().strftime("%Y%m%d")
    tmpl = overrides.get("run_id") or cfg.get("run_id", "{profile}-{agent}-{split}-{date}")
    steps = []
    for s in raw_steps:
        key = s.get("agent")
        if not key:
            raise ConfigError(f"profile {profile} 没有指定 agent")
        agent = ({"import_path": "eval.agents.replay:PatchReplayAgent", "grade": "replay"}
                 if key == "replay" else resolve_agent(runs, key, overrides.get("model")))
        run_id = tmpl.format(profile=profile, agent=key, split=split, date=today)
        if overrides.get("run_id") and len(raw_steps) > 1:
            run_id = f"{run_id}-{key}"
        repeats = int(overrides.get("repeats", s.get("repeats", cfg.get("repeats", 1))))
        steps.append(Step(key, agent, repeats, s.get("expect"), run_id))

    if cfg.get("grading_only") and not cfg.get("source_run"):
        raise ConfigError("grading_only 需要 source_run（要重新评分的 run_id）")
    if cfg.get("source_run") and _PLACEHOLDER.search(str(cfg["source_run"])):
        raise ConfigError("source_run 还是占位符，请用 --source-run 指定")

    return RunPlan(
        profile=profile, split=split, tasks=tasks, steps=steps,
        concurrency=int(cfg.get("concurrency", 1)),
        timeout_min=int(cfg.get("timeout_min", 180)),
        inline_verify=bool((cfg.get("grading") or {}).get("inline_verify", True)),
        keep_containers=bool(cfg.get("keep_containers", False)),
        environment=cfg.get("environment", "docker"),
        task_dirs=resolve_path(base, cfg.get("task_dirs") or runs["task_dirs"]),
        results_root=resolve_path(base, cfg.get("results_root") or runs["results_root"]),
        grading_only=bool(cfg.get("grading_only", False)),
        source_run=cfg.get("source_run"),
        bench_cfg=tasks_yaml.get("benchmarks") or {},
    )


def missing_env_vars(plan: RunPlan) -> list[str]:
    need = {n for s in plan.steps for n in env_templates(s.agent)}
    return sorted(n for n in need if not os.environ.get(n))
