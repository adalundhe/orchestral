import asyncio
import functools
import inspect
import pathlib
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Literal

from taskex import (
    Checkpoint,
    CheckpointStore,
    Env,
    MemoryCheckpointStore,
    Run,
    RunStatus,
    ShellProcess,
    SQLiteCheckpointStore,
    TaskRun,
    TaskRunner,
)
from taskex.snowflake import SnowflakeGenerator

from .step import ResumeFromInput, Step
from .step_type import StepType
from .util import get_shared_snowflake_generator

StepResult = ShellProcess | TaskRun

_TASKEX_RUN_RESERVED_KWARGS = frozenset(
    {
        "alias",
        "run_id",
        "timeout",
        "schedule",
        "trigger",
        "repeat",
        "keep",
        "max_age",
        "keep_policy",
        "resume_from",
    }
)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _kill_process_pool_workers(executor: ProcessPoolExecutor) -> None:
    """Best-effort kill of stuck ProcessPoolExecutor worker processes on forced teardown."""
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
    """Tracks an in-flight step execution delegated to ``taskex.TaskRunner``."""

    run_id: int
    task_name: str
    run: Run
    is_scheduled: bool = False
    task: asyncio.Task[StepResult] | None = None
    history: list[StepResult] = field(default_factory=list)

    @property
    def token(self) -> str:
        return self.run.token

    @property
    def status(self) -> RunStatus:
        return self.run.status


class WorkflowRunner:
    """Async workflow step runner backed by ``taskex.TaskRunner`` (>= 0.5.0).

    Provides lazy executor initialization, pause/resume (live and from
    checkpoints), checkpoint store integration, repeating schedule completion,
    fail-fast sibling cancellation, and leak-free shutdown/abort.
    """

    def __init__(
        self,
        instance_id: int = 0,
        config: Env | None = None,
        checkpoint_store: CheckpointStore | None = None,
    ) -> None:
        self._instance_id = instance_id
        self._config = config
        self._snowflake_generator = (
            get_shared_snowflake_generator()
            if instance_id == 0
            else SnowflakeGenerator(instance_id)
        )

        if checkpoint_store is not None:
            self._checkpoint_store: CheckpointStore = checkpoint_store
            self._owns_checkpoint_store = False
        elif config is not None and config.MERCURY_SYNC_CHECKPOINT_DB_PATH is not None:
            self._checkpoint_store = SQLiteCheckpointStore(
                config.MERCURY_SYNC_CHECKPOINT_DB_PATH
            )
            self._owns_checkpoint_store = True
        else:
            self._checkpoint_store = MemoryCheckpointStore()
            self._owns_checkpoint_store = True

        self._task_runner: TaskRunner | None = None
        self._active_runs: dict[str, RunHandle] = {}
        self._closed = False

    @property
    def checkpoint_store(self) -> CheckpointStore:
        return self._checkpoint_store

    @property
    def _executor(self) -> ThreadPoolExecutor | ProcessPoolExecutor | None:
        if self._task_runner is None:
            return None
        return self._task_runner._executor

    @property
    def active_tokens(self) -> list[str]:
        return list(self._active_runs)

    def create_task_id(self) -> int:
        return self._snowflake_generator.generate()

    def _ensure_task_runner(self, *, needs_sync_executor: bool = True) -> TaskRunner:
        if self._task_runner is not None and not self._task_runner._is_shutdown:
            if (
                needs_sync_executor
                and self._config is None
                and self._task_runner._executor is None
            ):
                # Default Env() uses ProcessPoolExecutor when sync tasks are needed;
                # only upgrade if no tasks are currently active.
                if not self._active_runs:
                    self._task_runner.abort()
                    self._task_runner = None
            else:
                return self._task_runner

        self._closed = False
        if self._config is not None:
            effective_config = self._config
        elif needs_sync_executor:
            effective_config = Env()
        else:
            effective_config = Env(MERCURY_SYNC_EXECUTOR_TYPE="none")

        self._task_runner = TaskRunner(
            instance_id=self._instance_id,
            config=effective_config,
            checkpoint_store=self._checkpoint_store,
        )
        self._task_runner._snowflake_generator = self._snowflake_generator
        return self._task_runner

    def prepare_for_steps(self, steps: list[Step[Any]]) -> None:
        """Lazily initialize the underlying TaskRunner tailored to the planned steps."""
        needs_sync = any(step.type == StepType.SYNC for step in steps)
        self._ensure_task_runner(needs_sync_executor=needs_sync)

    async def resolve_checkpoint(
        self,
        resume_from: ResumeFromInput,
    ) -> Checkpoint | None:
        """Resolve a checkpoint token, task name, or Checkpoint object (sync or async store)."""
        if resume_from is None:
            return None
        if isinstance(resume_from, Checkpoint):
            return resume_from

        if ":" in resume_from:
            task_name, run_id_str = resume_from.rsplit(":", maxsplit=1)
            try:
                run_id = int(run_id_str)
            except ValueError as err:
                raise ValueError(f"Invalid checkpoint token: {resume_from!r}") from err
            return await _maybe_await(
                self._checkpoint_store.get_latest(task_name, run_id)
            )

        return await _maybe_await(
            self._checkpoint_store.get_latest_for_task(resume_from)
        )

    def _launch_taskex_run(
        self,
        step: Step[Any],
        *,
        initial_checkpoint: Checkpoint | None = None,
    ) -> tuple[TaskRunner, Run, bool]:
        runner = self._ensure_task_runner(
            needs_sync_executor=(step.type == StepType.SYNC)
        )
        is_scheduled = step.repeat != "NEVER"
        effective_schedule: float | None = step.schedule_seconds
        if is_scheduled and effective_schedule is None:
            effective_schedule = 0.0

        # Ensure per-step repeat/schedule settings don't inherit stale Task settings
        # when the same task_name is reused with different parameters.
        existing_task = runner.tasks.get(step.name)
        if existing_task is not None:
            existing_task.repeat = step.repeat
            existing_task.schedule = effective_schedule
            existing_task.timeout = step.timeout_seconds
            existing_task.keep = 10 if step.keep is None else step.keep
            existing_task.max_age = step.max_age_seconds
            existing_task.keep_policy = step.keep_policy

        if step.type == StepType.SHELL:
            assert isinstance(step.call, str)
            env_map = (
                {str(k): str(v) for k, v in step.env.items()}
                if step.env is not None
                else None
            )
            run = runner.command(
                step.call,
                *step.shell_args,
                alias=step.name,
                env=env_map,
                cwd=step.cwd,
                shell=step.shell,
                timeout=step.timeout_seconds,
                schedule=effective_schedule,
                trigger=step.trigger,
                repeat=step.repeat,
                keep=step.keep,
                max_age=step.max_age_seconds,
                keep_policy=step.keep_policy,
                resume_from=initial_checkpoint,
            )
            return runner, run, is_scheduled

        has_reserved = any(k in _TASKEX_RUN_RESERVED_KWARGS for k in step.kwargs)
        if has_reserved:
            target = functools.partial(step.call, *step.args, **step.kwargs)
            call_args: tuple[Any, ...] = ()
            call_kwargs: dict[str, Any] = {}
        else:
            target = step.call
            call_args = step.args
            call_kwargs = step.kwargs

        run = runner.run(
            target,
            *call_args,
            alias=step.name,
            timeout=step.timeout_seconds,
            schedule=effective_schedule,
            trigger=step.trigger,
            repeat=step.repeat,
            keep=step.keep,
            max_age=step.max_age_seconds,
            keep_policy=step.keep_policy,
            resume_from=initial_checkpoint,
            **call_kwargs,
        )
        return runner, run, is_scheduled

    def run_step(
        self,
        step: Step[Any],
        *,
        initial_checkpoint: Checkpoint | None = None,
    ) -> RunHandle:
        """Schedule a ``Step`` on ``taskex.TaskRunner`` and return its ``RunHandle``."""
        runner, run, is_scheduled = self._launch_taskex_run(
            step,
            initial_checkpoint=initial_checkpoint,
        )
        handle = RunHandle(
            run_id=run.run_id,
            task_name=run.task_name,
            run=run,
            is_scheduled=is_scheduled,
        )
        handle.task = asyncio.create_task(self._await_step_handle(runner, handle, step))
        self._active_runs[handle.token] = handle
        return handle

    async def _await_step_handle(
        self,
        runner: TaskRunner,
        handle: RunHandle,
        step: Step[Any],
    ) -> StepResult:
        if not handle.is_scheduled:
            try:
                return await runner.wait(handle.token)
            except asyncio.CancelledError:
                await runner.cancel(handle.token)
                raise

        task_obj = runner.tasks[handle.task_name]
        schedule_id = handle.run_id
        schedule_task = task_obj._schedules.get(schedule_id)

        try:
            if schedule_task is not None:
                await schedule_task

            # Await any runs spawned by this scheduled step (snowflake IDs are monotonic)
            spawned = [
                r
                for rid, r in sorted(task_obj._runs.items())
                if rid >= schedule_id
            ]
            if spawned:
                results = await asyncio.gather(*(r.wait() for r in spawned))
            else:
                results = [await handle.run.wait()]

            await task_obj.cleanup()
            retained_ids = set(task_obj._runs.keys())
            retained_results = [res for res in results if res.run_id in retained_ids]
            handle.history = retained_results if retained_results else [results[-1]]
            return handle.history[-1]
        except asyncio.CancelledError:
            await runner.cancel_schedule(handle.token)
            raise

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
        resume_from: ResumeFromInput = None,
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
            resume_from=resume_from,
        )
        cp = resume_from if isinstance(resume_from, Checkpoint) else None
        return self.run_step(step, initial_checkpoint=cp)

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
        resume_from: ResumeFromInput = None,
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
            resume_from=resume_from,
        )
        cp = resume_from if isinstance(resume_from, Checkpoint) else None
        return self.run_step(step, initial_checkpoint=cp)

    async def pause(self, token: str) -> bool:
        if self._task_runner is None:
            return False
        handle = self._active_runs.get(token)
        if handle is not None and handle.is_scheduled:
            return await self._task_runner.pause_schedule(token)
        return await self._task_runner.pause(token)

    async def resume(self, token: str) -> bool:
        if self._task_runner is None:
            return False
        handle = self._active_runs.get(token)
        if handle is not None and handle.is_scheduled:
            return await self._task_runner.resume_schedule(token)
        if handle is not None:
            return await handle.run.resume()
        task_name, run_id = TaskRunner._parse_token(token)
        task = self._task_runner.tasks.get(task_name)
        if task is None:
            return False
        existing = task._runs.get(run_id)
        if existing is not None and existing.paused:
            return await existing.resume()
        return False

    async def pause_all(self) -> list[str]:
        paused_tokens: list[str] = []
        for token in list(self._active_runs):
            if await self.pause(token):
                paused_tokens.append(token)
        return paused_tokens

    async def resume_all(self) -> list[str]:
        resumed_tokens: list[str] = []
        for token in list(self._active_runs):
            if await self.resume(token):
                resumed_tokens.append(token)
        return resumed_tokens

    async def checkpoint(
        self,
        token: str,
        state: Any,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> Checkpoint:
        if self._task_runner is None:
            raise KeyError(f"No active runner for token {token!r}")
        return await self._task_runner.checkpoint(token, state, metadata=metadata)

    async def get_checkpoint(self, token: str) -> Checkpoint | None:
        if self._task_runner is not None:
            return await self._task_runner.get_checkpoint(token)
        return await self.resolve_checkpoint(token)

    async def get_latest_task_checkpoint(self, task_name: str) -> Checkpoint | None:
        if self._task_runner is not None:
            return await self._task_runner.get_latest_task_checkpoint(task_name)
        return await _maybe_await(
            self._checkpoint_store.get_latest_for_task(task_name)
        )

    async def list_checkpoints(self, token: str) -> list[Checkpoint]:
        if self._task_runner is not None:
            return await self._task_runner.list_checkpoints(token)
        task_name, run_id = TaskRunner._parse_token(token)
        return await _maybe_await(
            self._checkpoint_store.list_checkpoints(task_name, run_id)
        )

    async def clear_checkpoints(self, token: str) -> None:
        if self._task_runner is not None:
            await self._task_runner.clear_checkpoints(token)
            return
        task_name, run_id = TaskRunner._parse_token(token)
        await _maybe_await(self._checkpoint_store.delete(task_name, run_id))

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
        """Await runs in ``tokens``, cancelling remaining siblings on first failure."""
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
            if self._task_runner is not None:
                await self._task_runner.cancel(token)
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
        if self._task_runner is not None:
            if handle.is_scheduled:
                await self._task_runner.cancel_schedule(handle.token)
            else:
                await self._task_runner.cancel(handle.token)

        if handle.task is not None and not handle.task.done():
            handle.task.cancel()
            try:
                await handle.task
            except (asyncio.CancelledError, Exception):
                pass

    async def stop(self) -> None:
        tokens = list(self._active_runs)
        if tokens:
            await self.cancel_many(tokens)
        if self._task_runner is not None and not self._task_runner._is_shutdown:
            await self._task_runner.stop()

    async def shutdown(self) -> None:
        await self.stop()

        if self._task_runner is not None:
            runner = self._task_runner
            self._task_runner = None
            executor = runner._executor
            has_running_futures = any(
                not fut.done() for fut in list(runner._active_executor_futures)
            )
            await runner.shutdown()
            if has_running_futures and isinstance(executor, ProcessPoolExecutor):
                _kill_process_pool_workers(executor)

        if self._owns_checkpoint_store and not self._closed:
            try:
                await _maybe_await(self._checkpoint_store.close())
            except Exception:
                pass

        self._closed = True

    def abort(self) -> None:
        handles = list(self._active_runs.values())
        self._active_runs.clear()
        for handle in handles:
            if handle.task is not None and not handle.task.done():
                handle.task.cancel()

        if self._task_runner is not None:
            runner = self._task_runner
            self._task_runner = None
            executor = runner._executor
            has_running_futures = any(
                not fut.done() for fut in list(runner._active_executor_futures)
            )
            runner.abort()
            if has_running_futures and isinstance(executor, ProcessPoolExecutor):
                _kill_process_pool_workers(executor)

        if self._owns_checkpoint_store and not self._closed:
            try:
                res = self._checkpoint_store.close()
                if inspect.iscoroutine(res):
                    res.close()
            except Exception:
                pass

        self._closed = True
