"""runtime 重启后的对账（视图已经由 Runtime.open 从快照 + 重放重建）。

  0. 容器重建（rebuild=True，G4）：从原始代码重新初始化影子仓库（树哈希必须等于 0 号存档）→ 按顺序 unbundle 宿主机上的
     全部 bundle → 检查每个存档与快照的对象（最后一次导出之后的快照记为 lost；丢了提交的存档按确定的提交重做，
     连树都没有就把链截到还在的祖先）→ 恢复引用 → 工作区检出为最新一张已导出快照的原样树 → 重跑破坏探针。
  1. 存档链：处于 advancing 的尝试查 git 引用。已指向新提交 → 补记 created；还指向旧提交 → 重做 CAS（提交是确定的）；
     都不是 → cas_conflict。之后引用必须等于链头的提交，否则按链头修正（回退事件之后崩溃）。
  2. 作业：有完成标记的补收结果；进程组还活着的重新接上（G5，只在验证槽位与 live 作业上：它们不碰 worker 的工作区）；
     其余记为 unknown，由规则按同样的 (树, 检查集合) 重跑。
  3. 会话（G2）：容器还在、停机不久、同一会话恢复失败没有超过上限、轨迹读得出来 → 读盘重放（会话保持打开）；
     否则记为结束（runtime_crash / rebuild），从恢复点开新会话。
  4. 没做完的 LLM 副作用（诊断、复查）与定位 diff 重新发起。
  5. runtime_recovered（停机时长按墙钟；rebuilt、lost_snapshots、isolation），然后由主循环继续。
"""
from __future__ import annotations

import shlex
from pathlib import Path
from typing import TYPE_CHECKING

from belay.core import rules as R
from belay.core.effects import Effect
from belay.core.model import ATT_ADVANCING, JOB_RUNNING, WHERE_WORKSPACE
from belay.core.queries import chain, latest_snapshot
from belay.core.verify import test_files_of
from belay.runtime.gitops import SNAP_REF
from belay.runtime.session import load_transcript_messages

if TYPE_CHECKING:
    from belay.runtime.driver import BelayRun


async def rebuild_container(run: "BelayRun", report: dict) -> dict:
    rt, repo, env = run.rt, run.repo, run.env
    g = rt.graph
    dirs = [run.s.git_dir, f"{run.s.git_dir}.bundles"] + ([run.verifier.verify_dir] if run.verifier else [])
    await env.run("rm -rf " + " ".join(shlex.quote(d) for d in dirs), timeout=600, cwd="/")
    commit, tree = await repo.init()
    if tree != g.checkpoints[0].tree or commit != g.checkpoints[0].commit:
        raise RuntimeError(f"the rebuilt original code ({tree}) differs from checkpoint 0 ({g.checkpoints[0].tree})")
    meta = run.store.get_meta("mirror", {}) or {}
    remote_dir = f"{run.s.git_dir}.bundles"
    await env.run(f"mkdir -p {shlex.quote(remote_dir)}", timeout=30, cwd="/")
    for name in meta.get("bundles") or []:
        data = (Path(run.s.run_dir) / "git" / name).read_bytes()
        remote = f"{remote_dir}/{name}"
        await env.write_bytes(remote, data)
        await repo.bundle_unbundle(remote)
    # 快照：最后一次导出之后的丢了
    snaps = sorted(g.snapshots.values(), key=lambda s: s.n)
    have = await repo.has_objects([s.commit for s in snaps if s.commit])
    lost = [s.n for s in snaps if not s.commit or not have.get(s.commit)]
    for s in snaps:
        if s.n not in lost:
            await repo.update_ref(f"{SNAP_REF}{s.n}", s.commit)
    # 存档：提交是确定的；树还在就重做提交，连树都没有就把链截到还在的祖先
    missing_cps = []
    for cp in sorted(g.checkpoints.values(), key=lambda c: c.id):
        if cp.id == 0:
            continue
        if not (await repo.has_objects([cp.commit])).get(cp.commit):
            a = g.attempts.get(cp.attempt or "")
            if a is not None and (await repo.has_objects([cp.tree])).get(cp.tree) and a.parent_commit and \
                    a.date is not None and (await repo.has_objects([a.parent_commit])).get(a.parent_commit):
                redo = await repo.commit(cp.tree, a.parent_commit, run.commit_message(a.id), a.date)
                if redo == cp.commit:
                    await repo.set_cp_ref(cp.id, redo)
                    continue
            missing_cps.append(cp.id)
        else:
            await repo.set_cp_ref(cp.id, cp.commit)
    report["lost_checkpoints"] = missing_cps
    on_chain = [c.id for c in chain(g)]
    if any(c in missing_cps for c in on_chain):
        keep = next(c for c in on_chain if c not in missing_cps)
        for a in [a for a in rt.graph.attempts.values() if a.status == ATT_ADVANCING]:
            await rt.submit(R.ref_advanced, a.id, False, "", (), "lost when the container was rebuilt")
        await rt.submit(R.abort_attempts, "lost when the container was rebuilt")
        await rt.submit(R.rollback, run.w, keep)
    await repo.set_ref(rt.graph.head_cp.commit)
    # 工作区：最新一张已导出快照的原样树（含测试路径下的改动）
    snap = next((s for s in reversed(snaps) if s.n not in lost), None)
    target = snap.raw_tree if snap is not None else rt.graph.head_cp.tree
    await repo.checkout(target, run.w)
    report["workspace"] = f"snapshot {snap.n}" if snap is not None else "checkpoint"
    iso = None
    if run.verifier is not None and run.spec.test_cmd:
        await run.verifier.setup()
        guard_files = test_files_of({t: "pass" for t, c in rt.graph.baseline.items() if c == "pass"})
        probe = await run.verifier.isolation_probe(guard_files, rt.graph.checkpoints[0].tree)
        iso = {"probe_after_rebuild": probe}
        if not probe.get("ok"):
            iso.update(valid=False, reason=f"isolation probe after rebuild: {probe.get('reason')}")
    return {"lost": lost, "isolation": iso}


async def reconcile(run: "BelayRun", rebuild: bool = False) -> dict:
    rt = run.rt
    report: dict = {"attempts": [], "jobs": [], "sessions": [], "ref_fixed": False, "rebuilt": rebuild}
    alive = run.store.get_meta("alive", None)
    if alive is None:                               # 还没来得及写心跳：用最后一条事件的时间
        last = run.store.events(after=max(0, rt.graph.seq - 1))
        alive = last[-1].t if last else rt.now()
    downtime = max(0.0, rt.now() - alive)
    run.platform = (await run.env.run("uname -sm", timeout=30)).output.strip() or "Linux"
    rebuilt = {"lost": [], "isolation": None}
    if rebuild:
        rebuilt = await rebuild_container(run, report)

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
        out = None if rebuild or run.verifier is None else await run.verifier.collect(j.id)
        if out is not None:
            await rt.submit(R.job_finished, j.id, out.state, out.results, out.sec, out.error, out.reasons)
            report["jobs"].append({"job": j.id, "collected": True})
        elif not rebuild and run.verifier is not None and j.where != WHERE_WORKSPACE and \
                await run.verifier.alive(j.id):
            run.spawn(_reattach(run, j))                # G5：进程组还活着 → 重新接上，不重跑
            report["jobs"].append({"job": j.id, "reattached": True})
        else:
            if run.verifier is not None and not rebuild:
                await run.verifier.cancel(j.id)
            lost.append(j.id)
    for jid in lost:
        await rt.submit(R.job_finished, jid, "unknown", {}, 0.0, "lost when the runtime crashed")
        report["jobs"].append({"job": jid, "collected": False})

    # 3. 会话
    for w in list(rt.graph.workers.values()):
        if w.session is None:
            continue
        s = rt.graph.sessions[w.session]
        replays = sum(1 for m in s.resumes if m == "replay")
        ok = (not rebuild and downtime <= run.cfg.resume_max_downtime_sec and replays < run.cfg.resume_max_failures
              and s.transcript and load_transcript_messages(s.transcript, run.store.read_blob) is not None)
        if ok:
            await rt.submit(R.session_resumed, s.id, "replay", f"downtime {int(downtime)}s")
            report["sessions"].append({"session": s.id, "resumed": "replay"})
        else:
            await rt.submit(R.end_session, w.id, "rebuild" if rebuild else "runtime_crash")
            report["sessions"].append({"session": s.id, "resumed": None})

    # 4. 没做完的副作用
    g = rt.graph
    for d in g.diagnoses.values():
        if d.status == "requested":
            rt.spawn(Effect("diagnose", {"diagnosis": d.id}))
    for v in g.reviews.values():
        if v.status == "running":
            rt.spawn(Effect("review", {"review": v.id}))
    for loc in g.locates.values():
        if loc.status == "concluded" and len(loc.results) < len(loc.groups):
            rt.spawn(Effect("locate_diff", {"locate": loc.id, "groups": len(loc.groups)}))

    # 5.
    await rt.submit(R.recovered, downtime, report, rebuilt=rebuild, lost_snapshots=rebuilt["lost"],
                    isolation=rebuilt["isolation"])
    if rebuild and latest_snapshot(rt.graph) is not None:
        await run.mirror("rebuild")
    return report


async def _reattach(run: "BelayRun", job) -> None:
    out = await run.verifier.reattach(job)
    await run.rt.submit(R.job_finished, job.id, out.state, out.results, out.sec, out.error, out.reasons)
