"""runtime 重启后的对账（视图已经由 Runtime.open 从快照 + 重放重建）。

  1. 存档链：处于 advancing 的尝试查 git 引用。已指向新提交 → 补记 created；还指向旧提交 → 重做 CAS（提交是确定的，
     重做得到同一个提交）；都不是 → 记 cas_conflict。之后引用必须等于链头的提交，否则按链头修正（回退事件之后崩溃）。
  2. 作业：有完成标记的补收结果；其余杀掉进程组，记为 unknown，由规则按同样的 (树, 检查集合) 重跑。
  3. 会话：崩溃前在运行的会话记为结束（runtime_crash）；租约属于持久的 worker 身份，新会话开始时续期。
  4. 记 runtime_recovered（停机时长按墙钟），然后由主循环用 build_context 开新会话。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from belay.core import rules as R
from belay.core.model import ATT_ADVANCING, JOB_RUNNING

if TYPE_CHECKING:
    from belay.runtime.driver import BelayRun


async def reconcile(run: "BelayRun") -> dict:
    rt = run.rt
    report: dict = {"attempts": [], "jobs": [], "sessions": [], "ref_fixed": False}
    alive = run.store.get_meta("alive", None)
    downtime = rt.now() - alive if alive else 0.0
    run.platform = (await run.env.run("uname -sm", timeout=30)).output.strip() or "Linux"

    # 1. 存档链
    for a in [a for a in rt.graph.attempts.values() if a.status == ATT_ADVANCING]:
        ok, commit, files, detail = await run.advance(a.id)
        await rt.submit(R.ref_advanced, a.id, ok, commit, files, detail or "reconciled after restart")
        report["attempts"].append({"attempt": a.id, "ok": ok, "detail": detail})
    head = rt.graph.head_cp
    if head is not None and await run.repo.exists():
        ref = await run.repo.read_ref()
        if ref != head.commit:
            await run.repo.set_ref(head.commit)
            report["ref_fixed"] = True

    # 2. 作业（先把所有丢失的作业记下来，再提交：级联会起替代作业）
    lost = []
    for j in [j for j in rt.graph.jobs.values() if j.state == JOB_RUNNING]:
        out = await run.verifier.collect(j.id) if run.verifier is not None else None
        if out is not None:
            await rt.submit(R.job_finished, j.id, out.state, out.results, out.sec, out.error)
            report["jobs"].append({"job": j.id, "collected": True})
        else:
            if run.verifier is not None:
                await run.verifier.cancel(j.id)
            lost.append(j.id)
    for jid in lost:
        await rt.submit(R.job_finished, jid, "unknown", {}, 0.0, "lost when the runtime crashed")
        report["jobs"].append({"job": jid, "collected": False})

    # 3. 会话
    for w in list(rt.graph.workers.values()):
        if w.session is not None:
            report["sessions"].append(w.session)
            await rt.submit(R.end_session, w.id, "runtime_crash")

    # 4.
    await rt.submit(R.recovered, downtime, report)
    return report
