import asyncio
import sys
import time

import pytest
from taskex import Env
from taskex.models import RunStatus

from orchestral import Orchestral, OrchestralTaskFailure, WorkflowStatus


@pytest.mark.asyncio
async def test_fail_fast_true_wait_true_and_false():
    second_group_ran = False

    async def fail_fn():
        raise ValueError("boom in g1")

    async def g2_fn():
        nonlocal second_group_ran
        second_group_ran = True
        return "should not run"

    for wait_mode in (True, False):
        second_group_ran = False
        async with Orchestral() as orch:
            with orch as wf:
                g1 = wf.group()
                g1.add_task(fail_fn)
                g2 = wf.group()
                g2.add_task(g2_fn)

            await orch.run(wait=wait_mode, fail_fast=True)
            if not wait_mode:
                await orch.wait()

            assert orch.status == WorkflowStatus.FAILED
            assert isinstance(orch.error, OrchestralTaskFailure)
            assert "boom in g1" in str(orch.error)
            assert orch.error.task_name == "fail_fn"
            assert orch.trace is not None and "ValueError: boom in g1" in orch.trace
            assert orch.elapsed >= 0
            assert second_group_ran is False
            # Failed group's result must still be recorded for inspection
            assert "fail_fn" in orch.results
            assert next(iter(orch.results["fail_fn"].values())).status == RunStatus.FAILED


@pytest.mark.asyncio
async def test_fail_fast_cancels_slow_sibling_in_same_group():
    sibling_cancelled = False

    async def quick_fail():
        await asyncio.sleep(0.02)
        raise RuntimeError("fast error")

    async def slow_sibling():
        nonlocal sibling_cancelled
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            sibling_cancelled = True
            raise

    async with Orchestral() as orch:
        with orch as wf:
            g = wf.group()
            g.add_task(quick_fail)
            g.add_task(slow_sibling)

        t0 = time.monotonic()
        await orch.run(wait=True, fail_fast=True)
        elapsed = time.monotonic() - t0

        assert elapsed < 1.0
        assert sibling_cancelled is True
        assert orch.status == WorkflowStatus.FAILED
        assert isinstance(orch.error, OrchestralTaskFailure)


@pytest.mark.asyncio
async def test_fail_fast_does_not_trigger_on_zero_exit_stderr_warning():
    script = "import sys; sys.stderr.write('harmless warning\\n'); print('ok')"
    async with Orchestral() as orch:
        with orch as wf:
            g1 = wf.group()
            g1.add_command(sys.executable, "-c", script, alias="warn_cmd")
            g2 = wf.group()
            g2.add_command("echo", "after_warn", alias="after_cmd")

        await orch.run(wait=True, fail_fast=True)

        assert orch.status == WorkflowStatus.COMPLETED
        assert orch.error is None
        warn_res = next(iter(orch.results["warn_cmd"].values()))
        assert warn_res.status == RunStatus.COMPLETE
        assert warn_res.return_code == 0
        assert "harmless warning" in (warn_res.error or "")
        assert "after_cmd" in orch.results


@pytest.mark.asyncio
async def test_fail_fast_false_in_non_blocking_mode():
    """wait=False with fail_fast=False (default) must NOT silently default to fail_fast=True."""
    g2_ran = False

    async def fail_fn():
        raise RuntimeError("expected failure")

    async def after_fn():
        nonlocal g2_ran
        g2_ran = True
        return "ran"

    async with Orchestral() as orch:
        with orch as wf:
            g1 = wf.group()
            g1.add_task(fail_fn)
            g2 = wf.group()
            g2.add_task(after_fn)

        await orch.run(wait=False)
        await orch.wait()

        assert orch.status == WorkflowStatus.COMPLETED
        assert orch.error is None
        assert g2_ran is True
        assert next(iter(orch.results["after_fn"].values())).result == "ran"


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_mode", [True, False])
async def test_workflow_timeout_wait_true_and_false(wait_mode):
    async def sleeper():
        await asyncio.sleep(5)

    async with Orchestral() as orch:
        with orch as wf:
            g = wf.group()
            g.add_task(sleeper)

        t0 = time.monotonic()
        await orch.run(wait=wait_mode, timeout="100ms")
        if not wait_mode:
            await orch.wait()
        elapsed = time.monotonic() - t0

        assert elapsed < 1.0
        assert orch.status == WorkflowStatus.FAILED
        assert isinstance(orch.error, TimeoutError)
        assert orch.trace is not None
        assert orch.elapsed >= 0


@pytest.mark.asyncio
@pytest.mark.parametrize("zero_timeout", [0, 0.0, "0s"])
async def test_zero_timeout_is_not_ignored(zero_timeout):
    async def slow():
        await asyncio.sleep(1)

    async with Orchestral() as orch:
        with orch as wf:
            g = wf.group()
            g.add_task(slow)

        await orch.run(wait=True, timeout=zero_timeout)
        assert orch.status == WorkflowStatus.FAILED
        assert isinstance(orch.error, TimeoutError)


@pytest.mark.asyncio
async def test_streaming_aiter_multiple_groups_no_lost_batches():
    async def step(label: str) -> str:
        await asyncio.sleep(0.02)
        return label

    async with Orchestral() as orch:
        with orch as wf:
            g1 = wf.group()
            g1.add_task(step, "g1-a", alias="g1-a")
            g1.add_task(step, "g1-b", alias="g1-b")

            g2 = wf.group()
            g2.add_task(step, "g2-a", alias="g2-a")

            g3 = wf.group()
            g3.add_task(step, "g3-a", alias="g3-a")

        await orch.run(wait=False)

        # Two concurrent consumers both receive every batch in order
        async def consume():
            seen = []
            async for batch in orch:
                seen.append([r.task_name for r in batch])
            return seen

        c1, c2 = await asyncio.gather(consume(), consume())
        expected = [["g1-a", "g1-b"], ["g2-a"], ["g3-a"]]
        assert c1 == expected
        assert c2 == expected
        assert orch.status == WorkflowStatus.COMPLETED


@pytest.mark.asyncio
async def test_aiter_after_wait_true_and_started_before_run():
    async def step(n: int) -> int:
        await asyncio.sleep(0.01)
        return n

    async with Orchestral() as orch:
        with orch as wf:
            wf.group().add_task(step, 1, alias="s1")
            wf.group().add_task(step, 2, alias="s2")

        # Start consumer BEFORE run() starts
        async def consume():
            return [[r.result for r in batch] async for batch in orch]

        consumer_task = asyncio.create_task(consume())
        await asyncio.sleep(0.02)
        await orch.run(wait=True)

        assert await consumer_task == [[1], [2]]

        # Iterating again after completion yields all batches and terminates immediately
        replay = [[r.result for r in batch] async for batch in orch]
        assert replay == [[1], [2]]


@pytest.mark.asyncio
async def test_aiter_terminates_on_failure_and_timeout():
    async def boom():
        await asyncio.sleep(0.01)
        raise RuntimeError("nope")

    async with Orchestral() as orch:
        with orch as wf:
            wf.group().add_task(boom)
            wf.group().add_task(boom, alias="boom2")

        await orch.run(wait=False, fail_fast=True)
        batches = await asyncio.wait_for(_collect(orch), timeout=2.0)
        assert len(batches) == 1
        assert orch.status == WorkflowStatus.FAILED

        with orch as wf:
            wf.group().add_task(asyncio.sleep, 5, alias="hang")

        await orch.run(wait=False, timeout=0.05)
        batches = await asyncio.wait_for(_collect(orch), timeout=2.0)
        assert batches == []
        assert orch.status == WorkflowStatus.FAILED


async def _collect(orch: Orchestral):
    return [batch async for batch in orch]


@pytest.mark.asyncio
async def test_status_running_during_execution_and_state_reset_on_rerun():
    started = asyncio.Event()
    release = asyncio.Event()
    should_fail = True

    async def gated():
        started.set()
        await release.wait()
        if should_fail:
            raise ValueError("first run fails")
        return "ok"

    async with Orchestral() as orch:
        assert orch.status == WorkflowStatus.READY
        with orch as wf:
            wf.group().add_task(gated)

        await orch.run(wait=False, fail_fast=True)
        await started.wait()
        assert orch.status == WorkflowStatus.RUNNING
        assert orch.is_running is True

        # Concurrent run() must raise
        with pytest.raises(RuntimeError, match="already running"):
            await orch.run(wait=True)

        # create() while running must raise
        with pytest.raises(RuntimeError, match="currently running"):
            orch.create()

        release.set()
        await orch.wait()
        assert orch.status == WorkflowStatus.FAILED
        assert orch.error is not None
        assert orch.trace is not None

        # Re-run same workflow cleanly resets error, trace, results
        should_fail = False
        started.clear()
        release.set()
        await orch.run(wait=True, fail_fast=True)

        assert orch.status == WorkflowStatus.COMPLETED
        assert orch.error is None
        assert orch.trace is None
        assert len(orch.results["gated"]) == 1
        assert next(iter(orch.results["gated"].values())).result == "ok"


@pytest.mark.asyncio
async def test_config_preserved_across_enter_and_create():
    cfg = Env(
        MERCURY_SYNC_EXECUTOR_TYPE="thread",
        MERCURY_SYNC_TASK_RUNNER_MAX_THREADS=3,
    )
    orch = Orchestral(config=cfg)

    with orch as wf1:
        assert wf1._config is cfg
        wf1.group().add_command("echo", "1")
    await orch.run(wait=True)

    wf2 = orch.create()
    assert wf2._config is cfg
    assert orch.results == {}
    assert orch.error is None
    assert orch.status == WorkflowStatus.READY
    await orch.shutdown()


def test_construct_orchestral_and_build_workflow_outside_event_loop():
    """Must work cleanly before any event loop exists (Python 3.11-3.14)."""
    orch = Orchestral(config=Env(MERCURY_SYNC_EXECUTOR_TYPE="thread"))
    with orch as wf:
        g = wf.group()
        g.add_command("echo", "outside")
    assert orch.status == WorkflowStatus.READY
    assert wf.has_runner is False

    async def main():
        await orch.run(wait=True)
        assert orch.status == WorkflowStatus.COMPLETED
        await orch.shutdown()

    asyncio.run(main())
