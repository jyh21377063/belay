"""从 SWE-EVO 官方仓库导出每道题的评测配置，冻结为 sweevo_specs.json。

  python eval/convert/gen_sweevo_specs.py ../benchmarks/SWE-EVO [数据文件]

数据文件默认依次尝试 hf_out/hf_jsonl/swe-evo.jsonl 和 hf_out/hf_dataset/test/*.arrow。

运行环境需能 import 官方 SWE-bench 分支（pip install docker unidiff ghapi GitPython datasets）。
导出逻辑逐行对应官方 SWE-bench/evaluate_instance.py 与 swebench/harness/test_spec/python.py：
  - spec 版本：遍历 MAP_REPO_VERSION_TO_SPECS[repo] 的键，最后一个"是 end_version 子串"的键
    （官方实现如此；例如 dvc 0.53.1 会命中 "3.1"。为与论文结果可比，原样保留）
  - 测试文件：get_test_directives（test_patch 中的测试文件，去掉被删除的和非测试扩展名）
  - 测试命令：test_cmd 中第一个 pytest 替换为带 --continue-on-collection-errors 的版本
  - hot_fix：dask 早期版本评测时额外 pip install 的依赖
"""
import json
import sys
from pathlib import Path

repo = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(repo / "SWE-bench"))
from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS  # noqa: E402
from swebench.harness.log_parsers import MAP_REPO_TO_PARSER  # noqa: E402
from swebench.harness.test_spec.python import get_test_directives  # noqa: E402

PYTEST_FLAGS = ("pytest --continue-on-collection-errors -W default::DeprecationWarning "
                "-W default::pytest.PytestDeprecationWarning")



def load_rows():
    if len(sys.argv) > 2:
        path = Path(sys.argv[2])
    else:
        cands = [repo / "hf_out/hf_jsonl/swe-evo.jsonl", *sorted((repo / "hf_out/hf_dataset/test").glob("*.arrow"))]
        path = next(p for p in cands if p.exists())
    if path.suffix == ".arrow":
        import pyarrow as pa
        with pa.memory_map(str(path)) as src:
            try:
                return pa.ipc.open_stream(src).read_all().to_pylist()
            except pa.ArrowInvalid:
                return pa.ipc.open_file(src).read_all().to_pylist()
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


out = {}
for d in load_rows():
    d = dict(d)
    cur = d.get("end_version") or d.get("version")
    specs = MAP_REPO_VERSION_TO_SPECS.get(d["repo"], {})
    key = None
    for v in specs:
        if v in cur:
            key = v
    if key is None:
        out[d["instance_id"]] = {"error": f"找不到 spec（end_version={cur}）"}
        continue
    s = specs[key]
    d["version"] = key
    hot_fix = []
    if d["repo"] == "dask/dask" and int(d["start_version"].split(".")[0]) <= 2023:
        hot_fix.append("pip install 'pandas<2.0'")
        if d["end_version"] == "2023.6.1":
            hot_fix.append("pip install 'distributed==2023.6.0'")
    install = s.get("install", [])
    out[d["instance_id"]] = {
        "repo": d["repo"],
        "spec_version": key,
        "install": install if isinstance(install, list) else [install],
        "eval_commands": s.get("eval_commands", []),
        "hot_fix": hot_fix,
        "test_cmd": " ".join([s["test_cmd"], *get_test_directives(d)]).replace("pytest", PYTEST_FLAGS, 1),
        "log_parser": MAP_REPO_TO_PARSER[d["repo"]].__name__,
    }

dst = Path(__file__).with_name("sweevo_specs.json")
dst.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
print(f"{len(out)} 道题 → {dst}；失败 {sum('error' in v for v in out.values())} 道")
