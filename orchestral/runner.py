import asyncio
import concurrent.futures
import functools
import os
import pathlib
import shlex
import signal
import time
import traceback
from asyncio.subprocess import Process
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Literal

from taskex import Env
from taskex.models import RunStatus, ShellProcess, TaskRun, TaskType
from taskex.snowflake import SnowflakeGenerator

from .step import Step
from .step_type import StepType
from .util import is_async_callable

StepResult = ShellProcess | TaskRun

_IS_POSIX = os.name == "posix"
_TERMINATE_GRACE_SECONDS = 0.5


def _kill_process_sync(process: Process, *, force: bool = False) -> None:
    """Terminate or kill a subprocess (and its process group on POSIX) without raising."""
    if process.returncode is not None:
        return

    sig = signal.SIGKILL if force else signal.SIGTERM
    if _IS_POSIX and process.pid is not None:
        try:
            os.killpg(process.pid, sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass

    try:
        if force:
            process.kill()
        else:
            process.terminate()
    except (ProcessLookupError, OSError):
        pass


async def _terminate_and_reap_process(
    process: Process,
    *,
    grace_seconds: float = _TERMINATE_GRACE_SECONDS,
) -> None:
    """Gracefully terminate a subprocess, escalate to SIGKILL if needed, and reap it."""
    if process.returncode is not None:
        return

    _kill_process_sync(process, force=False)
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
    except asyncio.TimeoutError:
        _kill_process_sync(process, force=True)
        try:
            await process.wait()
        except (ProcessLookupError, OSError):
            pass
    except asyncio.CancelledError:
        _kill_process_sync(process, force=True)
        try:
            await process.wait()
        except BaseException:
            pass
        raise


def _kill_process_pool_workers(executor: ProcessPoolExecutor) -> None:
    """Best-effort kill of stuck ProcessPoolExecutor worker processes on abort."""
    processes = getattr(executor, "_processes", None)
    if not isinstance(processes, dict):
        return
    for proc in list(processes.values()):
        try:
            if proc.is_alive():
                proc.kill()
        except Exception:
            pass


@dataclass
class RunHandle:
    """Tracks an in-flight step execution inside WorkflowRunner."""

    run_id: int
    task_name: str
    status: RunStatus = RunStatus.CREATED
    start: float = field(default_factory=time.monotonic)
    task: asyncio.Task[StepResult] | None = None
    process: Process | None = None
    cf_future: concurrent.futures.Future[Any] | None = None
    history: list[StepResult] = field(default_factory=list)

    @property
    def token(self) -> str:
        return f"{self.task_name}:{self.run_id}"


class WorkflowRunner:
    """Leak-free async runner for shell, async, and sync workflow steps.

    Returns ``taskex.models.ShellProcess`` and ``taskex.models.TaskRun`` models
    and honors ``taskex.Env`` executor configuration without leaking executors,
    subprocesses, semaphores, asyncio tasks, or signal handlers.
    """

    def __init__(
        self,
        instance_id: int = 0,
        config: Env | None = None,
    ) -> None:
        self._config = config if config is not None else Env()
        self._snowflake_generator = SnowflakeGenerator(instance_id)
        self._executor: ThreadPoolExecutor | ProcessPoolExecutor | None = None
        self._executor_semaphore: asyncio.Semaphore | None = None
        self._semaphore_loop: asyncio.AbstractEventLoop | None = None
        self._active_runs: dict[str, RunHandle] = {}
        self._active_cf_futures: set[concurrent.futures.Future[Any]] = set()
        self._closed = False

    def create_task_id(self) -> int:
        return self._snowflake_generator.generate()

    def _max_workers(self) -> int:
        configured = self._config.MERCURY_SYNC_TASK_RUNNER_MAX_THREADS
        if configured is None or configured <= 0:
            return os.cpu_count() or 1
        return configured

    def _ensure_executor(
        self,
        loop: asyncio.AbstractEventLoop,
    ) -> tuple[ThreadPoolExecutor | ProcessPoolExecutor, asyncio.Semaphore]:
        if self._closed:
            self._closed = False

        max_workers = self._max_workers()
        if self._executor is None:
            if self._config.MERCURY_SYNC_EXECUTOR_TYPE == "thread":
                self._executor = ThreadPoolExecutor(max_workers=max_workers)
            else:
                self._executor = ProcessPoolExecutor(max_workers=max_workers)

        if self._executor_semaphore is None or self._semaphore_loop is not loop:
            self._executor_semaphore = asyncio.Semaphore(max_workers)
            self._semaphore_loop = loop

        return self._executor, self._executor_semaphore

    def run_step(self, step: Step[Any]) -> RunHandle:
        """Schedule a ``Step`` for immediate execution and return its ``RunHandle``."""
        self._closed = False
        run_id = self._snowflake_generator.generate()
        handle = RunHandle(run_id=run_id, task_name=step.name)
        handle.task = asyncio.create_task(self._execute_step(handle, step))
        self._active_runs[handle.token] = handle
        return handle

    def command(
        self,
        command: str,
        *args: str,
        alias: str | None = None,
        env: dict[str, Any] | None = None,
        cwd: str | pathlib.Path | None = None,
        shell: bool = False,
        timeout: str | int | float | None = None,
        schedule: str | int | float | None = None,
        trigger: Literal["MANUAL", "ON_START"] = "MANUAL",
        repeat: int | Literal["NEVER", "ALWAYS"] = "NEVER",
        keep: int | None = None,
        max_age: str | int | float | None = None,
        keep_policy: Literal["COUNT", "AGE", "COUNT_AND_AGE"] = "COUNT",
    ) -> RunHandle:
        step: Step[str] = Step(
            command,
            shell_args=args,
            env=env,
            cwd=str(cwd) if cwd is not None else None,
            shell=shell,
            alias=alias,
            timeout=timeout,
            schedule=schedule,
            trigger=trigger,
            repeat=repeat,
            keep=keep,
            max_age=max_age,
            keep_policy=keep_policy,
        )
        return self.run_step(step)

    def run(
        self,
        call: Any,
        *args: Any,
        alias: str | None = None,
        timeout: str | int | float | None = None,
        schedule: str | int | float | None = None,
        trigger: Literal["MANUAL", "ON_START"] = "MANUAL",
        repeat: int | Literal["NEVER", "ALWAYS"] = "NEVER",
        keep: int | None = None,
        max_age: str | int | float | None = None,
        keep_policy: Literal["COUNT", "AGE", "COUNT_AND_AGE"] = "COUNT",
        **kwargs: Any,
    ) -> RunHandle:
        step = Step(
            call,
            args=args,
            kwargs=kwargs,
            alias=alias,
            timeout=timeout,
            schedule=schedule,
            trigger=trigger,
            repeat=repeat,
            keep=keep,
            max_age=max_age,
            keep_policy=keep_policy,
        )
        return self.run_step(step)

    async def wait(self, token: str) -> StepResult | None:
        handle = self._active_runs.get(token)
        if handle is None or handle.task is None:
            return None

        try:
            return await handle.task
        except asyncio.CancelledError:
            if not handle.task.done():
                await self.cancel(token)
            raise
        finally:
            self._active_runs.pop(token, None)

    async def wait_all(self, tokens: list[str]) -> list[StepResult]:
        if not tokens:
            return []

        tasks = [
            handle.task
            for token in tokens
            if (handle := self._active_runs.get(token)) is not None
            and handle.task is not None
        ]
        if not tasks:
            return []

        try:
            results = await asyncio.gather(*tasks)
            return list(results)
        except BaseException:
            await self.cancel_many(tokens)
            raise
        finally:
            for token in tokens:
                self._active_runs.pop(token, None)

    async def wait_all_fail_fast(
        self,
        tokens: list[str],
    ) -> tuple[list[StepResult], StepResult | None]:
        """Await runs in ``tokens``, cancelling remaining siblings on first failure.

        Returns ``(completed_results, first_failed_result)`` with
        ``completed_results`` ordered according to ``tokens``.
        """
        if not tokens:
            return [], None

        token_by_task: dict[asyncio.Task[StepResult], str] = {}
        index_by_token: dict[str, int] = {}
        pending: set[asyncio.Task[StepResult]] = set()

        for idx, token in enumerate(tokens):
            handle = self._active_runs.get(token)
            if handle is not None and handle.task is not None:
                token_by_task[handle.task] = token
                index_by_token[token] = idx
                pending.add(handle.task)

        if not pending:
            return [], None

        completed_by_index: dict[int, StepResult] = {}
        first_failure: StepResult | None = None

        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                done_ordered = sorted(
                    done,
                    key=lambda t: index_by_token[token_by_task[t]],
                )
                for task in done_ordered:
                    token = token_by_task[task]
                    result = task.result()
                    completed_by_index[index_by_token[token]] = result
                    if first_failure is None and result.status == RunStatus.FAILED:
                        first_failure = result

                if first_failure is not None:
                    if pending:
                        pending_tokens = [token_by_task[t] for t in pending]
                        await self.cancel_many(pending_tokens)
                    break

        except BaseException:
            await self.cancel_many(tokens)
            raise
        finally:
            for token in tokens:
                self._active_runs.pop(token, None)

        ordered_results = [
            completed_by_index[i] for i in sorted(completed_by_index)
        ]
        return ordered_results, first_failure

    async def cancel(self, token: str) -> None:
        handle = self._active_runs.pop(token, None)
        if handle is None:
            return
        await self._cancel_handle(handle)

    async def cancel_many(self, tokens: list[str]) -> None:
        handles = [
            handle
            for token in tokens
            if (handle := self._active_runs.pop(token, None)) is not None
        ]
        if not handles:
            return
        await asyncio.gather(
            *(self._cancel_handle(handle) for handle in handles),
            return_exceptions=True,
        )

    async def _cancel_handle(self, handle: RunHandle) -> None:
        handle.status = RunStatus.CANCELLED
        if handle.cf_future is not None and not handle.cf_future.done():
            handle.cf_future.cancel()

        if handle.process is not None and handle.process.returncode is None:
            _kill_process_sync(handle.process, force=False)

        if handle.task is not None and not handle.task.done():
            handle.task.cancel()
            try:
                await handle.task
            except (asyncio.CancelledError, Exception):
                pass

        if handle.process is not None and handle.process.returncode is None:
            await _terminate_and_reap_process(handle.process)

        handle.process = None
        handle.cf_future = None

    async def stop(self) -> None:
        tokens = list(self._active_runs)
        if tokens:
            await self.cancel_many(tokens)

    async def shutdown(self) -> None:
        await self.stop()
        self._shutdown_executor(force=False)
        self._closed = True

    def abort(self) -> None:
        handles = list(self._active_runs.values())
        self._active_runs.clear()

        for handle in handles:
            handle.status = RunStatus.CANCELLED
            if handle.process is not None:
                _kill_process_sync(handle.process, force=True)
                handle.process = None
            if handle.cf_future is not None and not handle.cf_future.done():
                handle.cf_future.cancel()
                handle.cf_future = None
            if handle.task is not None and not handle.task.done():
                handle.task.cancel()

        self._shutdown_executor(force=True)
        self._closed = True

    def _shutdown_executor(self, *, force: bool) -> None:
        executor = self._executor
        if executor is None:
            return

        self._executor = None
        self._executor_semaphore = None
        self._semaphore_loop = None

        remaining_futures = [
            fut for fut in list(self._active_cf_futures) if not fut.done()
        ]
        for fut in remaining_futures:
            fut.cancel()
        has_running_futures = any(not fut.done() for fut in remaining_futures)
        self._active_cf_futures.clear()

        wait_for_workers = not has_running_futures
        try:
            executor.shutdown(wait=wait_for_workers, cancel_futures=True)
        except Exception:
            pass

        if (force or has_running_futures) and isinstance(executor, ProcessPoolExecutor):
            _kill_process_pool_workers(executor)

    async def _execute_step(
        self,
        handle: RunHandle,
        step: Step[Any],
    ) -> StepResult:
        if step.repeat == "NEVER":
            return await self._execute_once(handle, step, handle.run_id)

        interval = step.schedule_seconds or 0.0
        last_result: StepResult | None = None

        if step.repeat == "ALWAYS":
            iteration = 0
            while True:
                run_id = (
                    handle.run_id
                    if iteration == 0
                    else self._snowflake_generator.generate()
                )
                last_result = await self._execute_once(handle, step, run_id)
                self._record_and_prune_history(handle, step, last_result)
                iteration += 1
                await asyncio.sleep(interval)
        else:
            total_iterations = step.repeat
            for iteration in range(total_iterations):
                run_id = (
                    handle.run_id
                    if iteration == 0
                    else self._snowflake_generator.generate()
                )
                last_result = await self._execute_once(handle, step, run_id)
                self._record_and_prune_history(handle, step, last_result)
                if iteration + 1 < total_iterations and interval > 0:
                    await asyncio.sleep(interval)

        assert last_result is not None
        return last_result

    @staticmethod
    def _record_and_prune_history(
        handle: RunHandle,
        step: Step[Any],
        result: StepResult,
    ) -> None:
        handle.history.append(result)
        policy = step.keep_policy

        if policy in ("AGE", "COUNT_AND_AGE") and step.max_age_seconds is not None:
            now = time.monotonic()
            handle.history = [
                item
                for item in handle.history
                if (now - item.start) <= step.max_age_seconds
            ]

        if policy in ("COUNT", "COUNT_AND_AGE"):
            limit = step.keep if step.keep is not None else 10
            if len(handle.history) > limit:
                handle.history = handle.history[-limit:]

    async def _execute_once(
        self,
        handle: RunHandle,
        step: Step[Any],
        run_id: int,
    ) -> StepResult:
        if step.type == StepType.SHELL:
            return await self._execute_shell(handle, step, run_id)
        return await self._execute_callable(handle, step, run_id)

    async def _execute_shell(
        self,
        handle: RunHandle,
        step: Step[Any],
        run_id: int,
    ) -> ShellProcess:
        assert isinstance(step.call, str)
        start = time.monotonic()
        handle.start = start
        handle.status = RunStatus.RUNNING
        command_type: Literal["shell", "subprocess"] = (
            "shell" if step.shell else "subprocess"
        )
        cwd_str = str(step.cwd) if step.cwd is not None else None
        env_map = (
            {str(k): str(v) for k, v in step.env.items()}
            if step.env is not None
            else None
        )
        subprocess_kwargs: dict[str, Any] = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "env": env_map,
            "cwd": cwd_str,
        }
        if _IS_POSIX:
            subprocess_kwargs["start_new_session"] = True

        try:
            if step.shell:
                if step.shell_args:
                    quoted = [shlex.quote(arg) for arg in step.shell_args]
                    full_cmd = " ".join([step.call, *quoted])
                else:
                    full_cmd = step.call

                process = await asyncio.create_subprocess_shell(
                    full_cmd,
                    **subprocess_kwargs,
                )
            else:
                process = await asyncio.create_subprocess_exec(
                    step.call,
                    *step.shell_args,
                    **subprocess_kwargs,
                )
        except Exception as err:
            end = time.monotonic()
            handle.status = RunStatus.FAILED
            return ShellProcess(
                run_id=run_id,
                task_name=step.name,
                process_id=-1,
                command=step.call,
                args=step.shell_args,
                status=RunStatus.FAILED,
                return_code=None,
                env=step.env,
                working_directory=cwd_str,
                command_type=command_type,
                error=f"Err. - Task Run - {run_id} - encountered error {err}.",
                trace=traceback.format_exc(),
                start=start,
                end=end,
                elapsed=end - start,
                result=None,
                task_type=TaskType.SHELL,
            )

        handle.process = process
        pid = process.pid if process.pid is not None else -1

        try:
            if step.timeout_seconds is not None:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(),
                    timeout=step.timeout_seconds,
                )
            else:
                stdout_bytes, stderr_bytes = await process.communicate()

        except asyncio.TimeoutError:
            await _terminate_and_reap_process(process)
            end = time.monotonic()
            handle.status = RunStatus.FAILED
            return ShellProcess(
                run_id=run_id,
                task_name=step.name,
                process_id=pid,
                command=step.call,
                args=step.shell_args,
                status=RunStatus.FAILED,
                return_code=process.returncode,
                env=step.env,
                working_directory=cwd_str,
                command_type=command_type,
                error=(
                    f"Err. - Task Run - {run_id} - timed out. "
                    f"Exceeded deadline of - {step.timeout_seconds} - seconds."
                ),
                trace=None,
                start=start,
                end=end,
                elapsed=end - start,
                result=None,
                task_type=TaskType.SHELL,
            )

        except asyncio.CancelledError:
            handle.status = RunStatus.CANCELLED
            await _terminate_and_reap_process(process)
            raise

        except Exception as err:
            await _terminate_and_reap_process(process)
            end = time.monotonic()
            handle.status = RunStatus.FAILED
            return ShellProcess(
                run_id=run_id,
                task_name=step.name,
                process_id=pid,
                command=step.call,
                args=step.shell_args,
                status=RunStatus.FAILED,
                return_code=process.returncode,
                env=step.env,
                working_directory=cwd_str,
                command_type=command_type,
                error=f"Err. - Task Run - {run_id} - encountered error {err}.",
                trace=traceback.format_exc(),
                start=start,
                end=end,
                elapsed=end - start,
                result=None,
                task_type=TaskType.SHELL,
            )

        finally:
            handle.process = None

        end = time.monotonic()
        stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
        return_code = process.returncode

        if return_code != 0:
            status = RunStatus.FAILED
            error: str | None = (
                f"Err. - Task Run - {run_id} - failed. Encountered exception - {stderr}."
            )
        else:
            status = RunStatus.COMPLETE
            error = stderr if stderr else None

        handle.status = status
        return ShellProcess(
            run_id=run_id,
            task_name=step.name,
            process_id=pid,
            command=step.call,
            args=step.shell_args,
            status=status,
            return_code=return_code,
            env=step.env,
            working_directory=cwd_str,
            command_type=command_type,
            error=error,
            result=stdout,
            trace=None,
            start=start,
            end=end,
            elapsed=end - start,
            task_type=TaskType.SHELL,
        )

    async def _execute_callable(
        self,
        handle: RunHandle,
        step: Step[Any],
        run_id: int,
    ) -> TaskRun:
        start = time.monotonic()
        handle.start = start
        handle.status = RunStatus.RUNNING
        result: Any = None
        error: str | None = None
        trace: str | None = None
        status = RunStatus.COMPLETE

        try:
            if step.type == StepType.ASYNC or is_async_callable(step.call):
                coro = step.call(*step.args, **step.kwargs)
                if step.timeout_seconds is not None:
                    result = await asyncio.wait_for(coro, timeout=step.timeout_seconds)
                else:
                    result = await coro
            else:
                loop = asyncio.get_running_loop()
                executor, semaphore = self._ensure_executor(loop)
                target = (
                    functools.partial(step.call, *step.args, **step.kwargs)
                    if (step.args or step.kwargs)
                    else step.call
                )
                async with semaphore:
                    cf_future = executor.submit(target)
                    self._active_cf_futures.add(cf_future)
                    cf_future.add_done_callback(self._active_cf_futures.discard)
                    handle.cf_future = cf_future
                    aio_future = asyncio.wrap_future(cf_future, loop=loop)
                    try:
                        if step.timeout_seconds is not None:
                            result = await asyncio.wait_for(
                                aio_future,
                                timeout=step.timeout_seconds,
                            )
                        else:
                            result = await aio_future
                    except BaseException:
                        cf_future.cancel()
                        raise
                    finally:
                        handle.cf_future = None

        except asyncio.TimeoutError:
            error = (
                f"Err. - Task Run - {run_id} - timed out. "
                f"Exceeded deadline of - {step.timeout_seconds} - seconds."
            )
            status = RunStatus.FAILED

        except asyncio.CancelledError:
            handle.status = RunStatus.CANCELLED
            raise

        except Exception as err:
            error = f"Err. - Task Run - {run_id} - failed. Encountered exception - {err}."
            trace = traceback.format_exc()
            status = RunStatus.FAILED

        end = time.monotonic()
        handle.status = status
        return TaskRun(
            run_id=run_id,
            task_name=step.name,
            status=status,
            error=error,
            trace=trace,
            start=start,
            end=end,
            elapsed=end - start,
            result=result,
            task_type=TaskType.CALLABLE,
        )
