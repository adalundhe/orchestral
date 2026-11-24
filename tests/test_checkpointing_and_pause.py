import asyncio
import os
import sys
import tempfile
import threading
import time
from typing import Any

import pytest

from orchestral import (
    Checkpoint,
    Env,
    MemoryCheckpointStore,
    Orchestral,
    RunStatus,
    SQLiteCheckpointStore,
    WorkflowStatus,
    checkpoint,
    checkpoint_sync,
    get_run_context,
    last_checkpoint,
    pause_point,
    pause_point_sync,
)


class AsyncMemoryStore:
    """Custom async CheckpointStore implementation to verify async-store bridging."""

    def __init__(self) -> None:
        self._inner = MemoryCheckpointStore()
        self.closed = False

    async def save(self, cp: Checkpoint) -> None:
        await asyncio.sleep(0)
        self._inner.save(cp)

    async def get_latest(self, task_name: str, run_id: int) -> Checkpoint | None:
        await asyncio.sleep(0)
        return self._inner.get_latest(task_name, run_id)

    async def get_latest_for_task(self, task_name: str) -> Checkpoint | None:
        await asyncio.sleep(0)
        return self._inner.get_latest_for_task(task_name)

    async def list_checkpoints(self, task_name: str, run_id: int) -> list[Checkpoint]:
        await asyncio.sleep(0)
        return self._inner.list_checkpoints(task_name, run_id)

    async def delete(self, task_name: str, run_id: int) -> None:
        await asyncio.sleep(0)
        self._inner.delete(task_name, run_id)

    async def close(self) -> None:
        await asyncio.sleep(0)
        self.closed = True


@pytest.mark.asyncio
async def test_live_pause_and_resume_async_workflow():
    hit_item_1 = asyncio.Event()
    resumed_item_2 = asyncio.Event()
    processed: list[str] = []

    async def worker(items: list[str]) -> list[str]:
        ctx = get_run_context()
        assert ctx is not None
        for idx, item in enumerate(items):
            await pause_point()
            processed.append(item)
            await checkpoint({"index": idx + 1, "last": item})
            if idx == 0:
                hit_item_1.set()
                await asyncio.sleep(0.05)
            elif idx == 1:
                resumed_item_2.set()
        return list(processed)

    wave2_ran = False

    async def finish() -> str:
        nonlocal wave2_ran
        wave2_ran = True
        return "done"

    async with Orchestral() as orch:
        with orch as wf:
            s1 = wf.add_task(worker, ["a", "b", "c"], alias="worker")
            wf.add_task(finish, dependencies=[s1])

        await orch.run(wait=False)
        await asyncio.wait_for(hit_item_1.wait(), timeout=2.0)

        paused = await orch.pause()
        assert paused is True
        assert orch.status == WorkflowStatus.PAUSED
        assert orch.is_paused is True

        # While paused, item 2 and wave 2 must not proceed
        await asyncio.sleep(0.12)
        assert not resumed_item_2.is_set()
        assert wave2_ran is False
        assert orch.paused_duration >= 0.1

        # Resume and wait for completion
        await orch.resume(wait=True)

        assert orch.status == WorkflowStatus.COMPLETED
        assert orch.is_paused is False
        assert orch.paused_duration >= 0.1
        assert wave2_ran is True
        assert processed == ["a", "b", "c"]

        res = next(iter(orch.results["worker"].values()))
        assert res.status == RunStatus.COMPLETE
        assert res.result == ["a", "b", "c"]
        assert res.checkpoint is not None
        assert res.checkpoint.state == {"index": 3, "last": "c"}
        assert res.paused_duration > 0


@pytest.mark.asyncio
async def test_live_pause_and_resume_sync_thread_task():
    entered = threading.Event()
    proceeded_after_pause = threading.Event()

    def sync_worker(items: list[int]) -> int:
        total = 0
        for idx, item in enumerate(items):
            pause_point_sync()
            total += item
            checkpoint_sync({"index": idx + 1, "total": total})
            if idx == 0:
                entered.set()
                time.sleep(0.05)
            elif idx == 1:
                proceeded_after_pause.set()
        return total

    cfg = Env(MERCURY_SYNC_EXECUTOR_TYPE="thread", MERCURY_SYNC_TASK_RUNNER_MAX_THREADS=2)
    async with Orchestral(config=cfg) as orch:
        with orch as wf:
            wf.add_task(sync_worker, [10, 20, 30], alias="sync_worker")

        await orch.run(wait=False)
        while not entered.is_set():
            await asyncio.sleep(0.01)

        assert await orch.pause() is True
        assert orch.status == WorkflowStatus.PAUSED
        await asyncio.sleep(0.12)
        assert not proceeded_after_pause.is_set()

        await orch.resume(wait=True)
        assert orch.status == WorkflowStatus.COMPLETED
        res = next(iter(orch.results["sync_worker"].values()))
        assert res.result == 60
        assert res.checkpoint is not None
        assert res.checkpoint.state == {"index": 3, "total": 60}


@pytest.mark.asyncio
async def test_live_pause_and_resume_shell_subprocess_sigstop_sigcont():
    if os.name != "posix":
        pytest.skip("SIGSTOP/SIGCONT shell suspension requires POSIX")

    with tempfile.TemporaryDirectory() as tmpdir:
        marker = os.path.join(tmpdir, "ticks.log")
        script = (
            "import time\n"
            f"with open({marker!r}, 'a', buffering=1) as f:\n"
            "    for i in range(6):\n"
            "        f.write(f'{i}\\n')\n"
            "        time.sleep(0.04)\n"
        )

        async with Orchestral() as orch:
            with orch as wf:
                wf.add_command(sys.executable, "-c", script, alias="ticker")

            await orch.run(wait=False)
            # Wait for first tick to confirm process is running
            deadline = time.monotonic() + 2.0
            while not os.path.exists(marker) and time.monotonic() < deadline:
                await asyncio.sleep(0.01)

            assert await orch.pause() is True
            assert orch.status == WorkflowStatus.PAUSED
            await asyncio.sleep(0.03)
            with open(marker, encoding="utf-8") as fh:
                lines_while_paused_1 = len(fh.readlines())
            await asyncio.sleep(0.15)
            with open(marker, encoding="utf-8") as fh:
                lines_while_paused_2 = len(fh.readlines())

            # Process was frozen via SIGSTOP, so no new lines were written
            assert lines_while_paused_2 == lines_while_paused_1

            await orch.resume(wait=True)
            assert orch.status == WorkflowStatus.COMPLETED
            with open(marker, encoding="utf-8") as fh:
                assert len(fh.readlines()) == 6


@pytest.mark.asyncio
async def test_pause_aware_workflow_timeout_does_not_expire_while_paused():
    started = asyncio.Event()

    async def brief_work() -> str:
        started.set()
        await asyncio.sleep(0.04)
        await pause_point()
        await asyncio.sleep(0.04)
        return "finished"

    async with Orchestral() as orch:
        with orch as wf:
            wf.add_task(brief_work, alias="brief")

        # Workflow timeout is 0.25s, but we pause for 0.35s (> timeout!).
        # Because workflow timeout freezes while paused, it must still COMPLETE.
        await orch.run(wait=False, timeout=0.25)
        await asyncio.wait_for(started.wait(), timeout=2.0)

        assert await orch.pause() is True
        await asyncio.sleep(0.35)
        assert orch.status == WorkflowStatus.PAUSED

        await orch.resume(wait=True)
        assert orch.status == WorkflowStatus.COMPLETED
        assert orch.error is None
        assert next(iter(orch.results["brief"].values())).result == "finished"


@pytest.mark.asyncio
async def test_stop_and_shutdown_while_paused_unblocks_cleanly():
    started = asyncio.Event()

    async def wait_forever() -> None:
        started.set()
        while True:
            await pause_point()
            await asyncio.sleep(0.05)

    async with Orchestral() as orch:
        with orch as wf:
            wf.add_task(wait_forever, alias="sleeper")

        await orch.run(wait=False)
        await asyncio.wait_for(started.wait(), timeout=2.0)

        assert await orch.pause() is True
        assert orch.status == WorkflowStatus.PAUSED

        await asyncio.wait_for(orch.stop(), timeout=2.0)
        assert orch.status == WorkflowStatus.CANCELLED


@pytest.mark.asyncio
async def test_step_level_pause_and_resume_only_suspends_target_step():
    step_a_started = asyncio.Event()
    step_a_done = asyncio.Event()
    step_b_done = asyncio.Event()

    async def step_a() -> str:
        step_a_started.set()
        await asyncio.sleep(0.04)
        await pause_point()
        step_a_done.set()
        return "a"

    async def step_b() -> str:
        await asyncio.sleep(0.06)
        step_b_done.set()
        return "b"

    async with Orchestral() as orch:
        with orch as wf:
            sa = wf.add_task(step_a, alias="step_a")
            wf.add_task(step_b, alias="step_b")

        await orch.run(wait=False)
        await asyncio.wait_for(step_a_started.wait(), timeout=2.0)

        assert await orch.pause_step(sa) is True
        # step_b must finish even though step_a is paused
        await asyncio.wait_for(step_b_done.wait(), timeout=2.0)
        assert not step_a_done.is_set()

        assert await orch.resume_step("step_a") is True
        await orch.wait()

        assert orch.status == WorkflowStatus.COMPLETED
        assert step_a_done.is_set()


@pytest.mark.asyncio
async def test_workflow_resume_from_checkpoint_skips_completed_waves_and_recovers_step():
    step1_calls = 0
    step2_processed: list[str] = []
    fail_on_first_attempt = True

    async def ingest() -> list[str]:
        nonlocal step1_calls
        step1_calls += 1
        return ["x", "y", "z"]

    async def transform(items: list[str]) -> list[str]:
        prev = last_checkpoint()
        start_idx = prev.state["index"] if prev else 0
        done = list(prev.state["done"]) if prev else []

        for idx in range(start_idx, len(items)):
            if fail_on_first_attempt and idx == 2:
                raise RuntimeError("transient outage on item 2")
            val = items[idx].upper()
            done.append(val)
            step2_processed.append(val)
            await checkpoint({"index": idx + 1, "done": done})

        return done

    async def publish() -> str:
        return "published"

    async with Orchestral(workflow_id="etl-job") as orch:
        with orch as wf:
            s1 = wf.add_task(ingest)
            s2 = wf.add_task(transform, ["x", "y", "z"], dependencies=[s1])
            wf.add_task(publish, dependencies=[s2])

        # First run: ingest succeeds, transform fails on item 2 after checkpointing items 0 & 1
        await orch.run(wait=True, fail_fast=True)
        assert orch.status == WorkflowStatus.FAILED
        assert step1_calls == 1
        assert step2_processed == ["X", "Y"]
        assert "publish" not in orch.results

        cp = await orch.get_checkpoint("transform")
        assert cp is not None
        assert cp.state == {"index": 2, "done": ["X", "Y"]}

        # Fix the transient error and resume the workflow!
        fail_on_first_attempt = False
        await orch.resume(wait=True, fail_fast=True)

        assert orch.status == WorkflowStatus.COMPLETED
        assert orch.error is None
        # ingest was NOT re-executed (restored from workflow checkpoint)
        assert step1_calls == 1
        # transform resumed from item index 2 and only processed "Z"!
        assert step2_processed == ["X", "Y", "Z"]
        assert next(iter(orch.results["ingest"].values())).result == ["x", "y", "z"]
        assert next(iter(orch.results["transform"].values())).result == ["X", "Y", "Z"]
        assert next(iter(orch.results["publish"].values())).result == "published"


@pytest.mark.asyncio
async def test_cross_instance_sqlite_checkpoint_recovery_and_non_json_result():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "checkpoints.db")
        calls = {"prepare": 0, "work": []}

        class CustomObj:
            def __init__(self, label: str) -> None:
                self.label = label

        async def prepare() -> Any:
            calls["prepare"] += 1
            return CustomObj("non-json-safe")

        should_fail = True

        async def work(items: list[str]) -> list[str]:
            prev = last_checkpoint()
            idx = prev.state["idx"] if prev else 0
            acc = list(prev.state["acc"]) if prev else []
            for i in range(idx, len(items)):
                if should_fail and i == 1:
                    raise RuntimeError("crash mid-flight")
                acc.append(items[i])
                calls["work"].append(items[i])
                await checkpoint({"idx": i + 1, "acc": acc})
            return acc

        def build_wf(orch: Orchestral) -> None:
            with orch as wf:
                s1 = wf.add_task(prepare)
                wf.add_task(work, ["one", "two", "three"], dependencies=[s1])

        # Instance 1 backed by runner-owned SQLite DB path via Env
        cfg = Env(
            MERCURY_SYNC_EXECUTOR_TYPE="none",
            MERCURY_SYNC_CHECKPOINT_DB_PATH=db_path,
        )
        async with Orchestral(config=cfg, workflow_id="durable-pipe") as orch1:
            build_wf(orch1)
            await orch1.run(wait=True, fail_fast=True)
            assert orch1.status == WorkflowStatus.FAILED
            assert calls["prepare"] == 1
            assert calls["work"] == ["one"]

        # Instance 2 (simulating new process!) recovers from the same SQLite DB
        should_fail = False
        with SQLiteCheckpointStore(db_path) as store:
            async with Orchestral(
                config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none"),
                checkpoint_store=store,
                workflow_id="durable-pipe",
            ) as orch2:
                build_wf(orch2)
                await orch2.resume(wait=True, fail_fast=True)

                assert orch2.status == WorkflowStatus.COMPLETED
                # prepare was already completed in Instance 1, so NOT re-run!
                assert calls["prepare"] == 1
                # work resumed from index 1 -> only ran "two" and "three"!
                assert calls["work"] == ["one", "two", "three"]
                assert next(iter(orch2.results["work"].values())).result == [
                    "one",
                    "two",
                    "three",
                ]


@pytest.mark.asyncio
async def test_explicit_step_resume_from_and_async_checkpoint_store():
    store = AsyncMemoryStore()

    async def counter_step(target: int) -> int:
        prev = last_checkpoint()
        val = prev.state["val"] if prev else 0
        while val < target:
            val += 1
            await checkpoint({"val": val})
            if val == 2 and prev is None:
                raise RuntimeError("stop at 2")
        return val

    async with Orchestral(checkpoint_store=store) as orch:
        with orch as wf:
            wf.add_task(counter_step, 4, alias="counter")

        await orch.run(wait=True, fail_fast=True)
        assert orch.status == WorkflowStatus.FAILED
        first_run_id = next(iter(orch.results["counter"]))
        token = f"counter:{first_run_id}"

        cps = await orch.list_checkpoints(token)
        assert [c.state["val"] for c in cps] == [1, 2]

        # Build a fresh workflow passing explicit resume_from=token against an ASYNC store!
        with orch as wf2:
            wf2.add_task(counter_step, 4, alias="counter", resume_from=token)

        await orch.run(wait=True, fail_fast=True)
        assert orch.status == WorkflowStatus.COMPLETED
        assert next(iter(orch.results["counter"].values())).result == 4

        await orch.clear_checkpoints(token)
        assert await orch.list_checkpoints(token) == []

    # Caller owns externally passed checkpoint_store -> not auto-closed
    assert store.closed is False
    await store.close()


@pytest.mark.asyncio
async def test_external_checkpoint_api_and_clear_workflow_checkpoints():
    started = asyncio.Event()
    release = asyncio.Event()

    async def gated() -> str:
        started.set()
        await release.wait()
        return "done"

    async with Orchestral(workflow_id="ext-cp") as orch:
        with orch as wf:
            wf.add_task(gated, alias="gated")

        await orch.run(wait=False)
        await asyncio.wait_for(started.wait(), timeout=2.0)

        cp = await orch.checkpoint("gated", {"stage": "mid"}, metadata={"by": "test"})
        assert cp.state == {"stage": "mid"}
        assert cp.metadata == {"by": "test"}

        fetched = await orch.get_checkpoint("gated")
        assert fetched is not None
        assert fetched.checkpoint_id == cp.checkpoint_id

        release.set()
        await orch.wait()
        assert orch.status == WorkflowStatus.COMPLETED

        # Clear all workflow checkpoints and verify resume re-runs gated
        started.clear()
        release.set()
        await orch.clear_checkpoints()
        assert await orch.get_checkpoint("gated") is None

        await orch.resume(wait=True)
        assert started.is_set()
        assert orch.status == WorkflowStatus.COMPLETED


@pytest.mark.asyncio
async def test_rapid_sqlite_checkpoints_never_collide_on_snowflake_id():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "rapid.db")
        with SQLiteCheckpointStore(db_path) as store:
            for iteration in range(10):
                async def fast_cp(n: int) -> int:
                    await checkpoint({"n": n})
                    await checkpoint({"n": n + 1})
                    return n + 1

                async with Orchestral(
                    config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none"),
                    checkpoint_store=store,
                    workflow_id=f"rapid-{iteration}",
                ) as orch:
                    with orch as wf:
                        wf.add_task(fast_cp, iteration, alias="fast_cp")
                    await orch.run(wait=True, fail_fast=True)
                    assert orch.status == WorkflowStatus.COMPLETED
                    assert orch.error is None


@pytest.mark.asyncio
async def test_resume_rerun_completed_reexecutes_completed_steps():
    calls = {"a": 0, "b": 0}
    fail_b = True

    async def step_a() -> str:
        calls["a"] += 1
        return "a"

    async def step_b() -> str:
        calls["b"] += 1
        if fail_b:
            raise RuntimeError("b failed")
        return "b"

    async with Orchestral(workflow_id="rerun-test") as orch:
        with orch as wf:
            sa = wf.add_task(step_a)
            wf.add_task(step_b, dependencies=[sa])

        await orch.run(wait=True, fail_fast=True)
        assert orch.status == WorkflowStatus.FAILED
        assert calls == {"a": 1, "b": 1}

        fail_b = False
        await orch.resume(wait=True, fail_fast=True, rerun_completed=True)
        assert orch.status == WorkflowStatus.COMPLETED
        assert calls == {"a": 2, "b": 2}


@pytest.mark.asyncio
async def test_pause_and_resume_repeating_schedule_step():
    ticks = 0

    async def ticker() -> int:
        nonlocal ticks
        ticks += 1
        return ticks

    async with Orchestral() as orch:
        with orch as wf:
            wf.add_task(ticker, schedule="20ms", repeat=5, keep=5, alias="sched")

        await orch.run(wait=False)
        deadline = time.monotonic() + 2.0
        while ticks < 1 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)

        assert await orch.pause() is True
        assert orch.status == WorkflowStatus.PAUSED
        await asyncio.sleep(0.03)
        frozen_ticks = ticks
        await asyncio.sleep(0.10)
        assert ticks == frozen_ticks

        await orch.resume(wait=True)
        assert orch.status == WorkflowStatus.COMPLETED
        assert ticks == 5
