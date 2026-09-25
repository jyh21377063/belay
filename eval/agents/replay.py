"""评分用的"agent"：不调模型，只把保存的 patch.diff 应用到全新容器里。

应用之后由 Pier 照常运行任务自带的 verifier（tests/test.sh），
这就是 runs.yaml 里 grading.mode = replay 的实现：所有对比组走完全相同的评分路径。

commit=True 时，应用后再把改动提交（与 DeepSWE 的 solution/solve.sh 做法一致）。
DeepSWE 的 grader 只评"已提交"的内容：不提交会被当成空补丁，按原始代码评分。
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path

from pier.agents.base import BaseAgent

from eval.agents.patch_capture import GIT, PatchCaptureMixin


class PatchReplayAgent(PatchCaptureMixin, BaseAgent):
    def __init__(self, logs_dir: Path, model_name: str | None = None, patch_path: str = "",
                 repo_dir: str | None = None, commit: bool = False, **kwargs):
        kwargs.pop("extra_env", None)
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        self.patch_path = Path(patch_path)
        self.repo_dir = repo_dir
        self.commit = commit in (True, "true", "True", "1", 1)

    @staticmethod
    def name() -> str:
        return "belay-patch-replay"

    def version(self) -> str:
        return "1"

    async def setup(self, environment) -> None:
        return

    async def run(self, instruction, environment, context) -> None:
        report: dict = {"patch": str(self.patch_path)}
        if not self.patch_path.exists() or self.patch_path.stat().st_size == 0:
            report.update(ok=True, method="empty")
        else:
            repo = await self._belay_find_repo(environment)
            await environment.upload_file(self.patch_path, "/tmp/belay_submission.diff")
            # 与 SWE-bench 系列评估器一致：先 git apply，失败再用 patch 的模糊匹配兜底
            for method, cmd in [
                ("git_apply", f"{GIT} apply --binary --whitespace=nowarn /tmp/belay_submission.diff"),
                ("patch_fuzz", "patch --batch --fuzz=5 -p1 -i /tmp/belay_submission.diff"),
            ]:
                res = await self._belay_exec(environment, f"cd {shlex.quote(repo)} && {cmd}", check=False)
                report.setdefault("attempts", []).append(
                    {"method": method, "rc": res.return_code, "out": (res.stdout or "")[-2000:]})
                if res.return_code == 0:
                    report.update(ok=True, method=method)
                    break
            else:
                report.update(ok=False, method=None)   # 不抛异常：让 verifier 照常跑，结果自然是失败
            if report.get("ok") and self.commit:
                res = await self._belay_exec(environment, (
                    f"cd {shlex.quote(repo)} && {GIT} checkout -q -b belay/submission 2>/dev/null; "
                    f"{GIT} add -A && {GIT} -c user.name=belay -c user.email=belay@localhost "
                    f"commit -q --no-verify -m 'Apply submission'"), check=False)
                report["committed"] = res.return_code == 0
        (Path(self.logs_dir) / "apply.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
