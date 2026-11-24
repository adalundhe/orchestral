import asyncio
import functools
import os
import sys
import tempfile
import time

import pytest
from taskex import Env
from taskex.models import RunStatus, ShellProcess, TaskRun

from orchestral import Orchestral, WorkflowStatus


def top_level_square(x: int) -> int:
    return x * x


def top_level_add(a: int, b: int = 0) -> int:
    return a + b


def top_level_fail(msg: str) -> None:
    raise RuntimeError(msg)


def top_level_sleep_and_return(duration: float, value: str) -> str:
    time.sleep(duration)
    return value


class BoundHelper:
    def __init__(self, factor: int) -> None:
        self.factor = factor

    async def multiply_async(self, x: int) -> int:
        await asyncio.sleep(0.01)
        return x * self.factor

    def multiply_sync(self, x: int) -> int:
        return x * self.factor


class AsyncFunctor:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    async def __call__(self, text: str) -> str:
        return f"{self.prefix}:{text}"


@pytest.mark.asyncio
async def test_shell_commands_exec_shell_env_cwd():
    with tempfile.TemporaryDirectory() as tmpdir:
        async with Orchestral() as orch:
            with orch as wf:
                g = wf.group()
                g.add_command("echo", "hello world", alias="plain_echo")
                g.add_command("echo $CUSTOM_VAR", shell=True, env={"CUSTOM_VAR": "abc123", "PATH": os.environ.get("PATH", "")}, alias="shell_env")
                g.add_command("pwd", cwd=tmpdir, alias="check_cwd")

            await orch.run(wait=True)

            assert orch.status == WorkflowStatus.COMPLETED
            assert orch.error is None
            assert orch.elapsed >= 0

            echo_res = next(iter(orch.results["plain_echo"].values()))
            assert isinstance(echo_res, ShellProcess)
            assert echo_res.status == RunStatus.COMPLETE
            assert echo_res.result == "hello world\n"
            assert echo_res.return_code == 0
            assert echo_res.elapsed >= 0
            assert echo_res.end is not None and echo_res.end >= echo_res.start

            env_res = next(iter(orch.results["shell_env"].values()))
            assert env_res.status == RunStatus.COMPLETE
            assert env_res.result.strip() == "abc123"
            assert env_res.command_type == "shell"

            cwd_res = next(iter(orch.results["check_cwd"].values()))
            assert cwd_res.status == RunStatus.COMPLETE
            assert os.path.realpath(cwd_res.result.strip()) == os.path.realpath(tmpdir)


@pytest.mark.asyncio
async def test_shell_nonexistent_command_fails_cleanly():
    async with Orchestral() as orch:
        with orch as wf:
            g = wf.group()
            g.add_command("definitely_nonexistent_binary_orchestral_xyz")

        await orch.run(wait=True)

        assert orch.status == WorkflowStatus.COMPLETED
        res = next(iter(orch.results["definitely_nonexistent_binary_orchestral_xyz"].values()))
        assert isinstance(res, ShellProcess)
        assert res.status == RunStatus.FAILED
        assert res.process_id is None
        assert res.error is not None
        assert res.elapsed >= 0


@pytest.mark.asyncio
async def test_shell_large_stdout_no_pipe_deadlock_and_no_output_loss():
    """Subprocess writing >128KB over >100ms must neither deadlock nor lose output."""
    script = (
        "import sys, time; "
        "sys.stdout.write('A' * 131072); sys.stdout.flush(); "
        "time.sleep(0.05); "
        "sys.stdout.write('B' * 65536); sys.stdout.flush()"
    )
    async with Orchestral() as orch:
        with orch as wf:
            g = wf.group()
            g.add_command(sys.executable, "-c", script, alias="big_out")

        await orch.run(wait=True, timeout=5)

        assert orch.status == WorkflowStatus.COMPLETED
        res = next(iter(orch.results["big_out"].values()))
        assert res.status == RunStatus.COMPLETE
        assert len(res.result) == 131072 + 65536
        assert res.result.startswith("A" * 100)
        assert res.result.endswith("B" * 100)


@pytest.mark.asyncio
async def test_shell_non_utf8_output_decoded_safely():
    script = "import sys; sys.stdout.buffer.write(b'ok\\xff\\xfeend')"
    async with Orchestral() as orch:
        with orch as wf:
            g = wf.group()
            g.add_command(sys.executable, "-c", script, alias="bin_out")

        await orch.run(wait=True)
        res = next(iter(orch.results["bin_out"].values()))
        assert res.status == RunStatus.COMPLETE
        assert res.result.startswith("ok")
        assert res.result.endswith("end")


@pytest.mark.asyncio
async def test_async_tasks_same_function_different_args_and_reserved_kwargs():
    async def fetch(url: str, run_id: str = "none") -> str:
        await asyncio.sleep(0.01)
        return f"{url}:{run_id}"

    helper = BoundHelper(3)
    functor = AsyncFunctor("pre")

    async with Orchestral() as orch:
        with orch as wf:
            g = wf.group()
            g.add_task(fetch, "https://a.test", run_id="r1")
            g.add_task(fetch, "https://b.test", run_id="r2")
            g.add_task(helper.multiply_async, 7, alias="bound_async")
            g.add_task(functor, "val", alias="functor_async")
            g.add_task(functools.partial(fetch, "https://p.test", run_id="p1"), alias="partial_async")

        await orch.run(wait=True)

        assert orch.status == WorkflowStatus.COMPLETED
        fetch_values = sorted(r.result for r in orch.results["fetch"].values())
        assert fetch_values == ["https://a.test:r1", "https://b.test:r2"]

        bound_res = next(iter(orch.results["bound_async"].values()))
        assert isinstance(bound_res, TaskRun)
        assert bound_res.result == 21

        functor_res = next(iter(orch.results["functor_async"].values()))
        assert functor_res.result == "pre:val"

        partial_res = next(iter(orch.results["partial_async"].values()))
        assert partial_res.result == "https://p.test:p1"


@pytest.mark.asyncio
async def test_sync_tasks_thread_executor_and_no_semaphore_leak_on_failures():
    """Failing sync tasks exceeding max_workers must never leak semaphore permits or deadlock."""
    cfg = Env(
        MERCURY_SYNC_EXECUTOR_TYPE="thread",
        MERCURY_SYNC_TASK_RUNNER_MAX_THREADS=2,
    )
    helper = BoundHelper(4)

    async with Orchestral(config=cfg) as orch:
        with orch as wf:
            g1 = wf.group()
            for i in range(5):
                g1.add_task(top_level_fail, f"err-{i}", alias=f"fail_{i}")

            g2 = wf.group()
            g2.add_task(top_level_square, 9)
            g2.add_task(top_level_add, 10, b=5)
            g2.add_task(helper.multiply_sync, 6, alias="bound_sync")
            g2.add_task(lambda x: x + 1, 41, alias="lambda_sync")

        await orch.run(wait=True, timeout=5)

        assert orch.status == WorkflowStatus.COMPLETED
        for i in range(5):
            r = next(iter(orch.results[f"fail_{i}"].values()))
            assert r.status == RunStatus.FAILED
            assert f"err-{i}" in (r.error or "")
            assert r.trace is not None

        assert next(iter(orch.results["top_level_square"].values())).result == 81
        assert next(iter(orch.results["top_level_add"].values())).result == 15
        assert next(iter(orch.results["bound_sync"].values())).result == 24
        assert next(iter(orch.results["lambda_sync"].values())).result == 42


@pytest.mark.asyncio
async def test_sync_tasks_process_executor():
    cfg = Env(
        MERCURY_SYNC_EXECUTOR_TYPE="process",
        MERCURY_SYNC_TASK_RUNNER_MAX_THREADS=2,
    )
    async with Orchestral(config=cfg) as orch:
        with orch as wf:
            g = wf.group()
            g.add_task(top_level_square, 7)
            g.add_task(top_level_add, 20, b=22)

        await orch.run(wait=True, timeout=10)

        assert orch.status == WorkflowStatus.COMPLETED
        assert next(iter(orch.results["top_level_square"].values())).result == 49
        assert next(iter(orch.results["top_level_add"].values())).result == 42


@pytest.mark.asyncio
async def test_group_sequential_order_and_intra_group_concurrency():
    events: list[str] = []

    async def step_fn(name: str, delay: float) -> str:
        events.append(f"start:{name}")
        await asyncio.sleep(delay)
        events.append(f"end:{name}")
        return name

    async with Orchestral() as orch:
        with orch as wf:
            g1 = wf.group()
            g1.add_task(step_fn, "a", 0.08, alias="a")
            g1.add_task(step_fn, "b", 0.08, alias="b")

            g2 = wf.group()
            g2.add_task(step_fn, "c", 0.01, alias="c")

        t0 = time.monotonic()
        await orch.run(wait=True)
        elapsed = time.monotonic() - t0

        assert orch.status == WorkflowStatus.COMPLETED
        # a and b ran concurrently (~0.08s total rather than ~0.16s)
        assert elapsed < 0.15
        # c started strictly after both a and b ended
        assert events.index("start:c") > events.index("end:a")
        assert events.index("start:c") > events.index("end:b")


@pytest.mark.asyncio
async def test_empty_workflow_and_empty_groups():
    async with Orchestral() as orch:
        with orch as wf:
            wf.group()
            wf.group()

        await orch.run(wait=True)
        assert orch.status == WorkflowStatus.COMPLETED
        assert orch.error is None
        assert dict(orch.results) == {}

        batches = [batch async for batch in orch]
        assert batches == []


@pytest.mark.asyncio
async def test_repeating_step_with_keep_policy():
    counter = 0

    async def tick() -> int:
        nonlocal counter
        counter += 1
        return counter

    async with Orchestral() as orch:
        with orch as wf:
            g = wf.group()
            g.add_task(tick, schedule="10ms", repeat=5, keep=3, keep_policy="COUNT")

        await orch.run(wait=True, timeout=5)

        assert orch.status == WorkflowStatus.COMPLETED
        assert counter == 5
        runs = orch.results["tick"]
        assert len(runs) == 3
        assert sorted(r.result for r in runs.values()) == [3, 4, 5]


@pytest.mark.asyncio
async def test_step_level_timeouts_shell_async_sync():
    async def slow_async() -> str:
        await asyncio.sleep(5)
        return "late"

    cfg = Env(MERCURY_SYNC_EXECUTOR_TYPE="thread", MERCURY_SYNC_TASK_RUNNER_MAX_THREADS=2)
    async with Orchestral(config=cfg) as orch:
        with orch as wf:
            g = wf.group()
            g.add_command(sys.executable, "-c", "import time; time.sleep(5)", timeout="100ms", alias="slow_shell")
            g.add_task(slow_async, timeout=0.1, alias="slow_async")
            g.add_task(top_level_sleep_and_return, 0.3, "late", timeout=0.05, alias="slow_sync")

        t0 = time.monotonic()
        await orch.run(wait=True)
        elapsed = time.monotonic() - t0

        assert elapsed < 1.5
        assert orch.status == WorkflowStatus.COMPLETED
        for name in ("slow_shell", "slow_async", "slow_sync"):
            res = next(iter(orch.results[name].values()))
            assert res.status == RunStatus.FAILED
            assert "timed out" in (res.error or "")
