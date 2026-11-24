import asyncio
import os
import signal
import sys
import tempfile
import time

import pytest
from taskex import Env
from taskex.models import RunStatus

from orchestral import Orchestral, WorkflowStatus


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _blocking_sync_sleep(duration: float) -> str:
    time.sleep(duration)
    return "done"


async def _wait_until_pid_written(path: str, timeout: float = 3.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                content = fh.read().strip()
            if content.isdigit():
                return int(content)
        await asyncio.sleep(0.02)
    raise AssertionError(f"Timed out waiting for PID file {path}")


@pytest.mark.asyncio
async def test_no_leaked_asyncio_tasks_or_signal_handlers():
    before_sigint = signal.getsignal(signal.SIGINT)
    before_sigterm = signal.getsignal(signal.SIGTERM)
    before_sighup = signal.getsignal(signal.SIGHUP)

    async def noop():
        await asyncio.sleep(0.01)
        return 1

    async with Orchestral(config=Env(MERCURY_SYNC_EXECUTOR_TYPE="thread")) as orch:
        with orch as wf:
            g1 = wf.group()
            g1.add_command("echo", "hello")
            g1.add_task(noop)
            g1.add_task(lambda: 42, alias="sync_lambda")
        await orch.run(wait=True)

    await asyncio.sleep(0)
    current = asyncio.current_task()
    stray = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    assert stray == []
    assert signal.getsignal(signal.SIGINT) == before_sigint
    assert signal.getsignal(signal.SIGTERM) == before_sigterm
    assert signal.getsignal(signal.SIGHUP) == before_sighup


@pytest.mark.asyncio
async def test_pure_shell_and_async_workflow_never_spawns_executor():
    async def a():
        return 1

    async with Orchestral() as orch:
        with orch as wf:
            g = wf.group()
            g.add_command("echo", "no-executor")
            g.add_task(a)

        await orch.run(wait=True)
        assert wf.runner._executor is None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["timeout", "stop", "shutdown", "abort"])
async def test_no_leaked_grandchild_subprocesses_on_termination(action):
    """Shell command spawning a child process must have both parent & child killed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pid_file = os.path.join(tmpdir, "child.pid")
        # Spawn a background child sleep under sh and record the child's PID
        cmd = f"sleep 30 & echo $! > {pid_file}; wait"

        orch = Orchestral()
        with orch as wf:
            wf.group().add_command(cmd, shell=True, alias="spawn_child")

        if action == "timeout":
            await orch.run(wait=False, timeout=0.4)
            child_pid = await _wait_until_pid_written(pid_file)
            assert _pid_alive(child_pid)
            await orch.wait()
            assert orch.status == WorkflowStatus.FAILED
        else:
            await orch.run(wait=False)
            child_pid = await _wait_until_pid_written(pid_file)
            assert _pid_alive(child_pid)

            if action == "stop":
                await orch.stop()
            elif action == "shutdown":
                await orch.shutdown()
            elif action == "abort":
                orch.abort()
                await asyncio.sleep(0.05)

            assert orch.status == WorkflowStatus.CANCELLED

        # Allow OS brief moment to reap killed process
        deadline = time.monotonic() + 2.0
        while _pid_alive(child_pid) and time.monotonic() < deadline:
            await asyncio.sleep(0.02)

        assert not _pid_alive(child_pid), f"Child process {child_pid} leaked after {action}!"
        await orch.shutdown()

        await asyncio.sleep(0)
        current = asyncio.current_task()
        stray = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        assert stray == []


@pytest.mark.asyncio
async def test_abort_and_shutdown_idempotency_and_after_completed_shell():
    """abort() must never crash with AttributeError (no _run_task) or ProcessLookupError."""
    orch = Orchestral()
    # Before any run
    orch.abort()
    await orch.stop()
    await orch.shutdown()

    # After completed shell command (wait=True)
    with orch as wf:
        wf.group().add_command("echo", "done")
    await orch.run(wait=True)
    assert orch.status == WorkflowStatus.COMPLETED

    # Status must remain COMPLETED after shutdown/abort
    await orch.shutdown()
    assert orch.status == WorkflowStatus.COMPLETED
    await orch.shutdown()
    orch.abort()
    assert orch.status == WorkflowStatus.COMPLETED


@pytest.mark.asyncio
async def test_stop_and_abort_while_wait_true_is_running():
    async def forever():
        await asyncio.sleep(10)

    for use_abort in (False, True):
        orch = Orchestral()
        with orch as wf:
            wf.group().add_task(forever)

        runner_task = asyncio.create_task(orch.run(wait=True))
        await asyncio.sleep(0.05)
        assert orch.status == WorkflowStatus.RUNNING

        if use_abort:
            orch.abort()
        else:
            await orch.stop()

        await asyncio.wait_for(runner_task, timeout=2.0)
        assert orch.status == WorkflowStatus.CANCELLED
        await orch.shutdown()


@pytest.mark.asyncio
async def test_outer_task_cancellation_cleans_up_in_flight_subprocess():
    with tempfile.TemporaryDirectory() as tmpdir:
        pid_file = os.path.join(tmpdir, "outer.pid")
        cmd = f"sleep 30 & echo $! > {pid_file}; wait"

        orch = Orchestral()
        with orch as wf:
            wf.group().add_command(cmd, shell=True, alias="outer_child")

        outer = asyncio.create_task(orch.run(wait=True))
        child_pid = await _wait_until_pid_written(pid_file)
        assert _pid_alive(child_pid)

        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer

        deadline = time.monotonic() + 2.0
        while _pid_alive(child_pid) and time.monotonic() < deadline:
            await asyncio.sleep(0.02)

        assert not _pid_alive(child_pid)
        assert orch.status == WorkflowStatus.CANCELLED
        await orch.shutdown()


@pytest.mark.asyncio
async def test_abort_immediately_followed_by_new_run_no_state_corruption():
    async def slow():
        await asyncio.sleep(5)
        return "slow"

    async def fast():
        await asyncio.sleep(0.02)
        return "fast"

    async with Orchestral() as orch:
        with orch as wf1:
            wf1.group().add_task(slow)

        await orch.run(wait=False)
        # Abort synchronously and IMMEDIATELY start a new workflow without yielding!
        orch.abort()

        with orch as wf2:
            wf2.group().add_task(fast)

        await orch.run(wait=True)

        assert orch.status == WorkflowStatus.COMPLETED
        assert orch.error is None
        assert "fast" in orch.results
        assert "slow" not in orch.results
        assert next(iter(orch.results["fast"].values())).result == "fast"


@pytest.mark.asyncio
async def test_repeat_always_stopped_cleanly_without_memory_or_task_leak():
    count = 0

    async def pulse() -> int:
        nonlocal count
        count += 1
        return count

    async with Orchestral() as orch:
        with orch as wf:
            wf.group().add_task(pulse, schedule="10ms", repeat="ALWAYS", keep=2)

        await orch.run(wait=False)
        await asyncio.sleep(0.08)
        await orch.stop()

        assert orch.status == WorkflowStatus.CANCELLED
        assert count >= 2

    await asyncio.sleep(0)
    current = asyncio.current_task()
    stray = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    assert stray == []


@pytest.mark.asyncio
async def test_shutdown_does_not_deadlock_on_timed_out_sync_worker():
    cfg = Env(
        MERCURY_SYNC_EXECUTOR_TYPE="thread",
        MERCURY_SYNC_TASK_RUNNER_MAX_THREADS=1,
    )
    orch = Orchestral(config=cfg)
    with orch as wf:
        wf.group().add_task(_blocking_sync_sleep, 0.5, timeout=0.05, alias="hung_sync")

    t0 = time.monotonic()
    await orch.run(wait=True)
    await orch.shutdown()
    elapsed = time.monotonic() - t0

    assert elapsed < 0.35
    res = next(iter(orch.results["hung_sync"].values()))
    assert res.status == RunStatus.FAILED
    assert "timed out" in (res.error or "")


@pytest.mark.asyncio
async def test_workflow_replacement_cleans_up_previous_runner():
    cfg = Env(MERCURY_SYNC_EXECUTOR_TYPE="thread", MERCURY_SYNC_TASK_RUNNER_MAX_THREADS=1)
    orch = Orchestral(config=cfg)

    with orch as wf1:
        wf1.group().add_task(lambda: 1, alias="one")
    await orch.run(wait=True)
    old_executor = wf1.runner._executor
    assert old_executor is not None

    # Replace workflow WITHOUT explicitly calling shutdown() first -> old executor must be closed!
    with orch as wf2:
        wf2.group().add_command("echo", "two")

    assert old_executor._shutdown is True
    await orch.run(wait=True)
    await orch.shutdown()


@pytest.mark.asyncio
async def test_sigterm_ignoring_subprocess_escalated_to_sigkill():
    with tempfile.TemporaryDirectory() as tmpdir:
        pid_file = os.path.join(tmpdir, "stubborn.pid")
        script = (
            "import os, signal, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"open({pid_file!r}, 'w').write(str(os.getpid())); "
            "time.sleep(30)"
        )

        async with Orchestral() as orch:
            with orch as wf:
                wf.group().add_command(
                    sys.executable,
                    "-c",
                    script,
                    timeout="150ms",
                    alias="stubborn",
                )

            await orch.run(wait=False)
            pid = await _wait_until_pid_written(pid_file)
            await orch.wait()

            res = next(iter(orch.results["stubborn"].values()))
            assert res.status == RunStatus.FAILED
            assert "timed out" in (res.error or "")

            deadline = time.monotonic() + 2.0
            while _pid_alive(pid) and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert not _pid_alive(pid)


@pytest.mark.asyncio
async def test_repeated_runs_do_not_leak_file_descriptors():
    if not os.path.isdir("/dev/fd"):
        pytest.skip("/dev/fd not available on this platform")

    # Warm up once
    async with Orchestral(config=Env(MERCURY_SYNC_EXECUTOR_TYPE="thread")) as orch:
        with orch as wf:
            wf.group().add_command("echo", "warm")
            wf.group().add_task(lambda: 1, alias="sync_warm")
        await orch.run(wait=True)

    before_fds = len(os.listdir("/dev/fd"))

    for _ in range(25):
        async with Orchestral(config=Env(MERCURY_SYNC_EXECUTOR_TYPE="thread")) as orch:
            with orch as wf:
                g = wf.group()
                g.add_command("echo", "hello")
                g.add_task(lambda: 2, alias="sync_step")
            await orch.run(wait=True)
            assert orch.status == WorkflowStatus.COMPLETED

    after_fds = len(os.listdir("/dev/fd"))
    # Allow tiny noise from runtime/GC, fail on linear FD leak (25 iterations would leak 50+ FDs)
    assert after_fds - before_fds < 10, f"FD leak detected: before={before_fds}, after={after_fds}"


@pytest.mark.asyncio
async def test_process_pool_timeout_and_abort_no_hang():
    cfg = Env(
        MERCURY_SYNC_EXECUTOR_TYPE="process",
        MERCURY_SYNC_TASK_RUNNER_MAX_THREADS=1,
    )
    orch = Orchestral(config=cfg)
    with orch as wf:
        wf.group().add_task(_blocking_sync_sleep, 5.0, alias="proc_sleep")

    t0 = time.monotonic()
    await orch.run(wait=True, timeout=0.3)
    await orch.shutdown()
    elapsed = time.monotonic() - t0

    assert elapsed < 2.0
    assert orch.status == WorkflowStatus.FAILED
    assert isinstance(orch.error, TimeoutError)


@pytest.mark.asyncio
async def test_keep_policy_age_and_count_and_age():
    idx = 0

    async def produce() -> int:
        nonlocal idx
        idx += 1
        return idx

    async with Orchestral() as orch:
        with orch as wf:
            g = wf.group()
            g.add_task(
                produce,
                schedule="40ms",
                repeat=5,
                max_age="90ms",
                keep=2,
                keep_policy="COUNT_AND_AGE",
                alias="bounded",
            )

        await orch.run(wait=True, timeout=5)
        assert orch.status == WorkflowStatus.COMPLETED
        assert idx == 5
        assert len(orch.results["bounded"]) <= 2
