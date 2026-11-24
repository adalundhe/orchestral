import asyncio
import inspect
import json
import time
import traceback
from collections import defaultdict
from typing import Any, AsyncIterator

from taskex import (
    Checkpoint,
    CheckpointStore,
    Env,
    MemoryCheckpointStore,
    RunStatus,
    ShellProcess,
    SQLiteCheckpointStore,
    TaskRun,
    TaskType,
)

from .error import OrchestralTaskFailure
from .graph import ExecutionPlan
from .runner import RunHandle, StepResult, _maybe_await
from .step import Step
from .step_type import StepType
from .util import get_shared_snowflake_generator, parse_duration
from .workflow import Workflow
from .workflow_status import WorkflowStatus

_WORKFLOW_CHECKPOINT_PREFIX = "__orchestral__:workflow:"


def _is_json_serializable(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


def _serialize_step_result(res: StepResult) -> dict[str, Any]:
    raw_result = res.result
    if _is_json_serializable(raw_result):
        stored_result = raw_result
        result_serialized = True
    else:
        stored_result = None
        result_serialized = False

    payload: dict[str, Any] = {
        "task_name": res.task_name,
        "run_id": res.run_id,
        "task_type": res.task_type.value,
        "status": res.status.value,
        "result": stored_result,
        "result_serialized": result_serialized,
        "error": res.error if isinstance(res.error, str) else None,
        "trace": res.trace if isinstance(res.trace, str) else None,
        "start": float(res.start),
        "end": float(res.end) if res.end is not None else None,
        "elapsed": float(res.elapsed),
        "paused_duration": float(res.paused_duration),
    }

    if isinstance(res, ShellProcess):
        payload["command"] = res.command
        payload["args"] = list(res.args) if res.args is not None else None
        payload["return_code"] = res.return_code
        payload["process_id"] = res.process_id
        payload["working_directory"] = res.working_directory
        payload["command_type"] = res.command_type

    return payload


def _deserialize_step_result(data: dict[str, Any]) -> StepResult:
    task_type = TaskType(data["task_type"])
    status = RunStatus(data["status"])

    if task_type == TaskType.SHELL:
        raw_args = data.get("args")
        return ShellProcess(
            run_id=int(data["run_id"]),
            task_name=str(data["task_name"]),
            process_id=data.get("process_id"),
            command=str(data.get("command", "")),
            args=tuple(raw_args) if raw_args is not None else None,
            status=status,
            return_code=data.get("return_code"),
            working_directory=data.get("working_directory"),
            command_type=data.get("command_type", "subprocess"),
            error=data.get("error"),
            trace=data.get("trace"),
            start=float(data.get("start", 0.0)),
            end=float(data["end"]) if data.get("end") is not None else None,
            elapsed=float(data.get("elapsed", 0.0)),
            paused_duration=float(data.get("paused_duration", 0.0)),
            result=data.get("result"),
            task_type=TaskType.SHELL,
        )

    return TaskRun(
        run_id=int(data["run_id"]),
        task_name=str(data["task_name"]),
        status=status,
        error=data.get("error"),
        trace=data.get("trace"),
        start=float(data.get("start", 0.0)),
        end=float(data["end"]) if data.get("end") is not None else None,
        elapsed=float(data.get("elapsed", 0.0)),
        paused_duration=float(data.get("paused_duration", 0.0)),
        result=data.get("result"),
        task_type=TaskType.CALLABLE,
    )


def _compute_step_keys(plan: ExecutionPlan) -> dict[int, str]:
    counts: dict[str, int] = defaultdict(int)
    keys: dict[int, str] = {}
    for wave in plan.waves:
        for step in wave:
            occ = counts[step.name]
            counts[step.name] = occ + 1
            keys[id(step)] = f"{step.name}#{occ}"
    return keys


def _make_skipped_result(
    step: Step[Any],
    run_id: int,
    failed_dependencies: list[str],
) -> StepResult:
    now = time.monotonic()
    deps_label = ", ".join(failed_dependencies)
    message = f"Skipped step '{step.name}' because upstream dependency failed: {deps_label}"

    if step.type == StepType.SHELL:
        assert isinstance(step.call, str)
        return ShellProcess(
            run_id=run_id,
            task_name=step.name,
            process_id=None,
            command=step.call,
            args=step.shell_args,
            status=RunStatus.CANCELLED,
            return_code=None,
            env=step.env,
            working_directory=str(step.cwd) if step.cwd is not None else None,
            command_type="shell" if step.shell else "subprocess",
            error=message,
            trace=None,
            start=now,
            end=now,
            elapsed=0.0,
            paused_duration=0.0,
            result=None,
            task_type=TaskType.SHELL,
        )

    return TaskRun(
        run_id=run_id,
        task_name=step.name,
        status=RunStatus.CANCELLED,
        error=message,
        trace=None,
        start=now,
        end=now,
        elapsed=0.0,
        paused_duration=0.0,
        result=None,
        task_type=TaskType.CALLABLE,
    )


class Orchestral:
    def __init__(
        self,
        config: Env | None = None,
        checkpoint_store: CheckpointStore | None = None,
        workflow_id: str | None = None,
    ) -> None:
        self._config = config
        self._workflow_id = workflow_id if workflow_id is not None else "default"
        self._snowflake = get_shared_snowflake_generator()

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
        self._store_closed = False

        self._workflow = Workflow(
            config=config,
            checkpoint_store=self._checkpoint_store,
        )

        self._batches: list[list[StepResult]] = []
        self._batch_waiter = asyncio.Event()
        self._done = False
        self._run_task: asyncio.Task[None] | None = None
        self._run_generation = 0
        self._execute_next = False
        self._stop_requested = False

        self._resume_event = asyncio.Event()
        self._resume_event.set()
        self._pause_event = asyncio.Event()
        self._paused_at: float | None = None
        self._paused_duration: float = 0.0

        self._workflow_run_id: int = self._snowflake.generate()
        self._workflow_checkpoint_seq: int = 0
        self._completed_by_step_key: dict[str, StepResult] = {}
        self._step_run_refs: dict[str, tuple[str, int]] = {}
        self._active_step_tokens: dict[str, list[str]] = defaultdict(list)

        self.results: dict[str, dict[int, StepResult]] = defaultdict(dict)
        self.error: Exception | None = None
        self.trace: str | None = None
        self._start: float = 0.0
        self.elapsed: float = 0.0

    def __enter__(self) -> Workflow:
        return self.create()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        return None

    async def __aenter__(self) -> "Orchestral":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.shutdown()

    @property
    def status(self) -> WorkflowStatus:
        return self._workflow.status

    @property
    def workflow(self) -> Workflow:
        return self._workflow

    @property
    def workflow_id(self) -> str:
        return self._workflow_id

    @property
    def checkpoint_store(self) -> CheckpointStore:
        return self._checkpoint_store

    @property
    def is_running(self) -> bool:
        return self._run_task is not None and not self._run_task.done()

    @property
    def is_paused(self) -> bool:
        return self._workflow.status == WorkflowStatus.PAUSED

    @property
    def paused_duration(self) -> float:
        if self._paused_at is not None:
            return self._paused_duration + (time.monotonic() - self._paused_at)
        return self._paused_duration

    @property
    def active_tokens(self) -> list[str]:
        if not self._workflow.has_runner:
            return []
        return self._workflow.runner.active_tokens

    @property
    def _workflow_checkpoint_task_name(self) -> str:
        return f"{_WORKFLOW_CHECKPOINT_PREFIX}{self._workflow_id}"

    def execution_order(self) -> list[list[str]]:
        """Validate the active workflow's dependency graph and return wave-by-wave step names."""
        return self._workflow.execution_order()

    def _notify_waiters(self) -> None:
        waiter = self._batch_waiter
        self._batch_waiter = asyncio.Event()
        waiter.set()

    def _finalize_pause_accounting(self) -> None:
        if self._paused_at is not None:
            self._paused_duration += time.monotonic() - self._paused_at
            self._paused_at = None
        self._pause_event.clear()
        self._resume_event.set()

    def _reset_run_state(self, *, clear_checkpoint_cache: bool = True) -> None:
        self._run_generation += 1
        self.results = defaultdict(dict)
        self.error = None
        self.trace = None
        self._start = 0.0
        self.elapsed = 0.0
        self._batches = []
        self._done = False
        self._execute_next = False
        self._stop_requested = False
        self._paused_at = None
        self._paused_duration = 0.0
        self._pause_event.clear()
        self._resume_event.set()
        self._active_step_tokens.clear()
        if clear_checkpoint_cache:
            self._completed_by_step_key.clear()
            self._step_run_refs.clear()
        self._notify_waiters()

    async def __aiter__(self) -> AsyncIterator[list[StepResult]]:
        idx = 0
        while True:
            while idx < len(self._batches):
                yield list(self._batches[idx])
                idx += 1

            if self._done:
                break

            waiter = self._batch_waiter
            await waiter.wait()

    def _ensure_checkpoint_store(self) -> CheckpointStore:
        if self._owns_checkpoint_store and self._store_closed:
            if (
                self._config is not None
                and self._config.MERCURY_SYNC_CHECKPOINT_DB_PATH is not None
            ):
                self._checkpoint_store = SQLiteCheckpointStore(
                    self._config.MERCURY_SYNC_CHECKPOINT_DB_PATH
                )
            else:
                self._checkpoint_store = MemoryCheckpointStore()
            self._store_closed = False
            self._workflow._checkpoint_store = self._checkpoint_store
        return self._checkpoint_store

    def create(self) -> Workflow:
        if self.is_running:
            raise RuntimeError(
                "Cannot create a new workflow while a workflow is currently running"
            )

        if self._workflow.has_runner:
            self._workflow.runner.abort()

        store = self._ensure_checkpoint_store()
        self._run_task = None
        self._reset_run_state()
        self._workflow = Workflow(
            config=self._config,
            checkpoint_store=store,
        )
        return self._workflow

    async def _save_workflow_checkpoint(self, status: WorkflowStatus) -> None:
        if self._store_closed:
            return
        self._workflow_checkpoint_seq += 1
        cp = Checkpoint(
            checkpoint_id=self._snowflake.generate(),
            run_id=self._workflow_run_id,
            task_name=self._workflow_checkpoint_task_name,
            sequence=self._workflow_checkpoint_seq,
            state={
                "workflow_id": self._workflow_id,
                "status": status.value,
                "completed_steps": {
                    key: _serialize_step_result(res)
                    for key, res in self._completed_by_step_key.items()
                },
                "step_run_refs": {
                    key: [task_name, run_id]
                    for key, (task_name, run_id) in self._step_run_refs.items()
                },
            },
            metadata={"workflow_status": status.value},
            status=self._workflow_status_to_run_status(status),
        )
        try:
            await _maybe_await(self._checkpoint_store.save(cp))
        except Exception:
            pass

    @staticmethod
    def _workflow_status_to_run_status(status: WorkflowStatus) -> RunStatus:
        mapping = {
            WorkflowStatus.READY: RunStatus.CREATED,
            WorkflowStatus.RUNNING: RunStatus.RUNNING,
            WorkflowStatus.PAUSED: RunStatus.PAUSED,
            WorkflowStatus.COMPLETED: RunStatus.COMPLETE,
            WorkflowStatus.FAILED: RunStatus.FAILED,
            WorkflowStatus.CANCELLED: RunStatus.CANCELLED,
        }
        return mapping[status]

    async def _load_workflow_checkpoint_state(
        self,
    ) -> tuple[dict[str, StepResult], dict[str, tuple[str, int]]]:
        completed = dict(self._completed_by_step_key)
        run_refs = dict(self._step_run_refs)

        if self._store_closed:
            return completed, run_refs

        stored = await _maybe_await(
            self._checkpoint_store.get_latest_for_task(
                self._workflow_checkpoint_task_name
            )
        )
        if stored is None or not isinstance(stored.state, dict):
            return completed, run_refs

        stored_completed = stored.state.get("completed_steps", {})
        if isinstance(stored_completed, dict):
            for key, raw_res in stored_completed.items():
                if key not in completed and isinstance(raw_res, dict):
                    try:
                        completed[key] = _deserialize_step_result(raw_res)
                    except Exception:
                        pass

        stored_refs = stored.state.get("step_run_refs", {})
        if isinstance(stored_refs, dict):
            for key, pair in stored_refs.items():
                if (
                    key not in run_refs
                    and isinstance(pair, (list, tuple))
                    and len(pair) == 2
                ):
                    run_refs[key] = (str(pair[0]), int(pair[1]))

        return completed, run_refs

    async def run(
        self,
        wait: bool = False,
        fail_fast: bool = False,
        timeout: str | int | float | None = None,
    ) -> None:
        if self.is_running:
            raise RuntimeError("Workflow is already running")

        self._ensure_checkpoint_store()
        timeout_seconds = parse_duration(timeout, name="timeout")
        workflow = self._workflow
        plan = workflow.build_plan()

        self._reset_run_state(clear_checkpoint_cache=True)
        self._workflow_run_id = self._snowflake.generate()
        self._workflow_checkpoint_seq = 0
        generation = self._run_generation
        self._start = time.monotonic()
        self._execute_next = True
        workflow.status = WorkflowStatus.RUNNING

        task = asyncio.create_task(
            self._execute_workflow(
                generation=generation,
                workflow=workflow,
                plan=plan,
                fail_fast=fail_fast,
                timeout=timeout_seconds,
                resume_from_checkpoints=False,
                prior_completed={},
                prior_run_refs={},
            )
        )
        self._run_task = task

        if wait:
            await self._await_run_task(task)

    async def pause(self) -> bool:
        """Suspend a running workflow, freezing its timeout and pausing active steps."""
        if not self.is_running:
            return False
        if self._workflow.status == WorkflowStatus.PAUSED:
            return True
        if self._workflow.status != WorkflowStatus.RUNNING:
            return False

        self._paused_at = time.monotonic()
        self._resume_event.clear()
        self._pause_event.set()
        self._workflow.status = WorkflowStatus.PAUSED

        if self._workflow.has_runner:
            await self._workflow.runner.pause_all()

        await self._save_workflow_checkpoint(WorkflowStatus.PAUSED)
        return True

    async def resume(
        self,
        wait: bool = False,
        fail_fast: bool = False,
        timeout: str | int | float | None = None,
        *,
        rerun_completed: bool = False,
    ) -> None:
        """Resume a paused workflow live, or recover an incomplete workflow from checkpoints.

        - If the workflow is currently ``WorkflowStatus.PAUSED``, resumes all paused
          steps and continues executing remaining waves. When ``wait=True``, awaits
          workflow completion.
        - Otherwise (after failure, cancellation, or in a new ``Orchestral`` backed
          by a shared ``CheckpointStore``), skips already-completed steps (unless
          ``rerun_completed=True``) and launches remaining steps seeded with their
          latest persisted checkpoints so ``last_checkpoint()`` returns them.
        """
        if self._workflow.status == WorkflowStatus.PAUSED and self.is_running:
            if self._paused_at is not None:
                self._paused_duration += time.monotonic() - self._paused_at
                self._paused_at = None

            self._workflow.status = WorkflowStatus.RUNNING
            self._pause_event.clear()

            if self._workflow.has_runner:
                await self._workflow.runner.resume_all()

            self._resume_event.set()
            await self._save_workflow_checkpoint(WorkflowStatus.RUNNING)

            if wait and self._run_task is not None:
                await self._await_run_task(self._run_task)
            return

        if self.is_running:
            raise RuntimeError("Workflow is already running")

        self._ensure_checkpoint_store()
        timeout_seconds = parse_duration(timeout, name="timeout")
        workflow = self._workflow
        plan = workflow.build_plan()

        prior_completed, prior_run_refs = await self._load_workflow_checkpoint_state()
        if rerun_completed:
            prior_completed = {}

        self._reset_run_state(clear_checkpoint_cache=False)
        self._completed_by_step_key = dict(prior_completed)
        self._step_run_refs = dict(prior_run_refs)
        self._workflow_run_id = self._snowflake.generate()
        self._workflow_checkpoint_seq = 0
        generation = self._run_generation
        self._start = time.monotonic()
        self._execute_next = True
        workflow.status = WorkflowStatus.RUNNING

        task = asyncio.create_task(
            self._execute_workflow(
                generation=generation,
                workflow=workflow,
                plan=plan,
                fail_fast=fail_fast,
                timeout=timeout_seconds,
                resume_from_checkpoints=True,
                prior_completed=prior_completed,
                prior_run_refs=prior_run_refs,
            )
        )
        self._run_task = task

        if wait:
            await self._await_run_task(task)

    def _resolve_active_tokens(self, target: str | Step[Any]) -> list[str]:
        if not self._workflow.has_runner:
            return []
        active = self._workflow.runner.active_tokens
        if isinstance(target, Step):
            name = target.name
            return [t for t in active if t.rsplit(":", maxsplit=1)[0] == name]
        if ":" in target and target in active:
            return [target]
        return [t for t in active if t.rsplit(":", maxsplit=1)[0] == target]

    async def pause_step(self, target: str | Step[Any]) -> bool:
        """Pause one active step (by token, step name/alias, or Step reference)."""
        tokens = self._resolve_active_tokens(target)
        if not tokens:
            return False
        results = [await self._workflow.runner.pause(tok) for tok in tokens]
        return any(results)

    async def resume_step(self, target: str | Step[Any]) -> bool:
        """Resume one paused step (by token, step name/alias, or Step reference)."""
        tokens = self._resolve_active_tokens(target)
        if not tokens:
            return False
        results = [await self._workflow.runner.resume(tok) for tok in tokens]
        return any(results)

    async def checkpoint(
        self,
        target: str | Step[Any],
        state: Any,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> Checkpoint:
        """Save an external checkpoint for a running step (by token, name/alias, or Step)."""
        if not self._workflow.has_runner:
            raise RuntimeError("No active workflow runner")
        if isinstance(target, str) and ":" in target:
            return await self._workflow.runner.checkpoint(
                target,
                state,
                metadata=metadata,
            )
        tokens = self._resolve_active_tokens(target)
        if not tokens:
            label = target.name if isinstance(target, Step) else target
            raise KeyError(f"No active run found for step {label!r}")
        return await self._workflow.runner.checkpoint(
            tokens[-1],
            state,
            metadata=metadata,
        )

    async def get_checkpoint(
        self,
        target: str | Step[Any],
    ) -> Checkpoint | None:
        """Fetch the latest checkpoint by run token, step name/alias, or Step."""
        name_or_token = target.name if isinstance(target, Step) else target
        if ":" in name_or_token:
            if self._workflow.has_runner:
                return await self._workflow.runner.get_checkpoint(name_or_token)
            task_name, run_id_str = name_or_token.rsplit(":", maxsplit=1)
            return await _maybe_await(
                self._checkpoint_store.get_latest(task_name, int(run_id_str))
            )

        if self._workflow.has_runner:
            return await self._workflow.runner.get_latest_task_checkpoint(name_or_token)
        return await _maybe_await(
            self._checkpoint_store.get_latest_for_task(name_or_token)
        )

    async def list_checkpoints(self, token: str) -> list[Checkpoint]:
        """Return all checkpoints recorded for a specific run token."""
        if self._workflow.has_runner:
            return await self._workflow.runner.list_checkpoints(token)
        task_name, run_id_str = token.rsplit(":", maxsplit=1)
        return await _maybe_await(
            self._checkpoint_store.list_checkpoints(task_name, int(run_id_str))
        )

    async def clear_checkpoints(
        self,
        target: str | Step[Any] | None = None,
    ) -> None:
        """Clear checkpoints for a token, a step's runs, or the entire workflow."""
        if target is None:
            stored = await _maybe_await(
                self._checkpoint_store.get_latest_for_task(
                    self._workflow_checkpoint_task_name
                )
            )
            if stored is not None:
                await _maybe_await(
                    self._checkpoint_store.delete(
                        self._workflow_checkpoint_task_name,
                        stored.run_id,
                    )
                )
            for task_name, run_id in list(self._step_run_refs.values()):
                await _maybe_await(
                    self._checkpoint_store.delete(task_name, run_id)
                )
            self._completed_by_step_key.clear()
            self._step_run_refs.clear()
            return

        if isinstance(target, str) and ":" in target:
            if self._workflow.has_runner:
                await self._workflow.runner.clear_checkpoints(target)
            else:
                task_name, run_id_str = target.rsplit(":", maxsplit=1)
                await _maybe_await(
                    self._checkpoint_store.delete(task_name, int(run_id_str))
                )
            return

        step_name = target.name if isinstance(target, Step) else target
        for run_id in list(self.results.get(step_name, {})):
            await _maybe_await(self._checkpoint_store.delete(step_name, run_id))
        for key, (tname, rid) in list(self._step_run_refs.items()):
            if tname == step_name:
                await _maybe_await(self._checkpoint_store.delete(tname, rid))
                self._step_run_refs.pop(key, None)
                self._completed_by_step_key.pop(key, None)

    async def wait(self) -> None:
        """Wait for an in-progress non-blocking workflow run to finish."""
        task = self._run_task
        if task is None:
            return
        await self._await_run_task(task)

    async def _await_run_task(self, task: asyncio.Task[None]) -> None:
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if self._stop_requested and (current is None or current.cancelling() == 0):
                return

            if not task.done():
                self._execute_next = False
                self._finalize_pause_accounting()
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            raise

    async def _wait_if_paused(self) -> None:
        while not self._resume_event.is_set():
            await self._resume_event.wait()

    async def _await_with_pause_aware_timeout(
        self,
        target: asyncio.Task[None],
        timeout: float,
    ) -> None:
        remaining = float(timeout)
        loop = asyncio.get_running_loop()

        try:
            while True:
                if self._pause_event.is_set():
                    resume_waiter = loop.create_task(self._resume_event.wait())
                    try:
                        done, _ = await asyncio.wait(
                            {target, resume_waiter},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    finally:
                        if not resume_waiter.done():
                            resume_waiter.cancel()
                            try:
                                await resume_waiter
                            except asyncio.CancelledError:
                                pass

                    if target in done:
                        target.result()
                        return
                    continue

                if remaining <= 0:
                    if not target.done():
                        target.cancel()
                        try:
                            await target
                        except asyncio.CancelledError:
                            pass
                    raise asyncio.TimeoutError

                started = time.monotonic()
                pause_waiter = loop.create_task(self._pause_event.wait())
                try:
                    done, _ = await asyncio.wait(
                        {target, pause_waiter},
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    if not pause_waiter.done():
                        pause_waiter.cancel()
                        try:
                            await pause_waiter
                        except asyncio.CancelledError:
                            pass

                if target in done:
                    target.result()
                    return

                remaining -= time.monotonic() - started
                if pause_waiter in done:
                    continue

                if not target.done():
                    target.cancel()
                    try:
                        await target
                    except asyncio.CancelledError:
                        pass
                raise asyncio.TimeoutError

        except BaseException:
            if not target.done():
                target.cancel()
                try:
                    await target
                except (asyncio.CancelledError, Exception):
                    pass
            raise

    async def _execute_workflow(
        self,
        *,
        generation: int,
        workflow: Workflow,
        plan: ExecutionPlan,
        fail_fast: bool = False,
        timeout: float | None = None,
        resume_from_checkpoints: bool = False,
        prior_completed: dict[str, StepResult] | None = None,
        prior_run_refs: dict[str, tuple[str, int]] | None = None,
    ) -> None:
        try:
            coro = self._run_workflow(
                generation=generation,
                workflow=workflow,
                plan=plan,
                fail_fast=fail_fast,
                resume_from_checkpoints=resume_from_checkpoints,
                prior_completed=prior_completed or {},
                prior_run_refs=prior_run_refs or {},
            )
            if timeout is not None:
                wf_task = asyncio.create_task(coro)
                await self._await_with_pause_aware_timeout(wf_task, timeout)
            else:
                await coro

        except asyncio.TimeoutError:
            if generation == self._run_generation:
                self._finalize_pause_accounting()
                self.elapsed = time.monotonic() - self._start
                self.trace = traceback.format_exc()
                self.error = TimeoutError(
                    f"Err. - Workflow exceeded timeout of {timeout} seconds"
                )
                workflow.status = WorkflowStatus.FAILED
                await self._save_workflow_checkpoint(WorkflowStatus.FAILED)

        except asyncio.CancelledError:
            if generation == self._run_generation:
                self._finalize_pause_accounting()
                self.elapsed = time.monotonic() - self._start
                if workflow.status in (WorkflowStatus.RUNNING, WorkflowStatus.PAUSED):
                    workflow.status = WorkflowStatus.CANCELLED
                await self._save_workflow_checkpoint(workflow.status)
            raise

        except OrchestralTaskFailure as err:
            if generation == self._run_generation:
                self._finalize_pause_accounting()
                self.elapsed = time.monotonic() - self._start
                self.trace = err.trace or traceback.format_exc()
                self.error = err
                workflow.status = WorkflowStatus.FAILED
                await self._save_workflow_checkpoint(WorkflowStatus.FAILED)

        except Exception as err:
            if generation == self._run_generation:
                self._finalize_pause_accounting()
                self.elapsed = time.monotonic() - self._start
                self.trace = traceback.format_exc()
                self.error = err
                workflow.status = WorkflowStatus.FAILED
                await self._save_workflow_checkpoint(WorkflowStatus.FAILED)

        finally:
            if generation == self._run_generation:
                self._done = True
                self._notify_waiters()

    async def _resolve_step_checkpoint(
        self,
        step: Step[Any],
        step_key: str,
        *,
        resume_from_checkpoints: bool,
        prior_run_refs: dict[str, tuple[str, int]],
        runner: Any,
    ) -> Checkpoint | None:
        if step.resume_from is not None:
            return await runner.resolve_checkpoint(step.resume_from)

        if not resume_from_checkpoints:
            return None

        if step_key in prior_run_refs:
            task_name, prev_run_id = prior_run_refs[step_key]
            cp = await _maybe_await(
                self._checkpoint_store.get_latest(task_name, prev_run_id)
            )
            if cp is not None:
                return cp

        return await _maybe_await(
            self._checkpoint_store.get_latest_for_task(step.name)
        )

    async def _run_workflow(
        self,
        *,
        generation: int,
        workflow: Workflow,
        plan: ExecutionPlan,
        fail_fast: bool = False,
        resume_from_checkpoints: bool = False,
        prior_completed: dict[str, StepResult] | None = None,
        prior_run_refs: dict[str, tuple[str, int]] | None = None,
    ) -> None:
        prior_completed = prior_completed or {}
        prior_run_refs = prior_run_refs or {}
        step_keys = _compute_step_keys(plan)
        failed_step_ids: set[int] = set()

        all_steps = [step for wave in plan.waves for step in wave]
        if all_steps:
            workflow.runner.prepare_for_steps(all_steps)

        for wave in plan.waves:
            await self._wait_if_paused()
            if not self._execute_next or generation != self._run_generation:
                break

            if not wave:
                continue

            runner = workflow.runner
            runnable: list[tuple[int, Step[Any], str, Checkpoint | None]] = []
            results_by_pos: dict[int, StepResult] = {}

            for pos, step in enumerate(wave):
                step_key = step_keys[id(step)]

                if resume_from_checkpoints and step_key in prior_completed:
                    results_by_pos[pos] = prior_completed[step_key]
                    continue

                upstream_ids = plan.upstream_step_ids.get(id(step), ())
                failed_upstreams = [
                    plan.step_by_id[uid].name
                    for uid in upstream_ids
                    if uid in failed_step_ids
                ]
                if failed_upstreams:
                    failed_step_ids.add(id(step))
                    results_by_pos[pos] = _make_skipped_result(
                        step,
                        runner.create_task_id(),
                        failed_upstreams,
                    )
                else:
                    initial_cp = await self._resolve_step_checkpoint(
                        step,
                        step_key,
                        resume_from_checkpoints=resume_from_checkpoints,
                        prior_run_refs=prior_run_refs,
                        runner=runner,
                    )
                    runnable.append((pos, step, step_key, initial_cp))

            handles: list[tuple[int, Step[Any], str, RunHandle]] = []
            wave_tokens: list[str] = []
            failed: StepResult | None = None

            if runnable:
                try:
                    for pos, step, step_key, initial_cp in runnable:
                        handle = runner.run_step(step, initial_checkpoint=initial_cp)
                        handles.append((pos, step, step_key, handle))
                        wave_tokens.append(handle.token)
                        self._step_run_refs[step_key] = (
                            handle.task_name,
                            handle.run_id,
                        )

                    if self._pause_event.is_set():
                        await runner.pause_all()

                    if fail_fast:
                        runnable_results, failed = await runner.wait_all_fail_fast(
                            wave_tokens
                        )
                    else:
                        runnable_results = await runner.wait_all(wave_tokens)
                except BaseException:
                    if wave_tokens:
                        await runner.cancel_many(wave_tokens)
                    raise

                if generation != self._run_generation:
                    return

                handle_by_run_id = {
                    h.run_id: (pos, step, step_key, h)
                    for pos, step, step_key, h in handles
                }
                for res in runnable_results:
                    entry = handle_by_run_id.get(res.run_id)
                    if entry is None:
                        for pos, step, step_key, h in handles:
                            if any(hr.run_id == res.run_id for hr in h.history):
                                entry = (pos, step, step_key, h)
                                break
                    if entry is not None:
                        pos, step, step_key, _ = entry
                        results_by_pos[pos] = res
                        self._step_run_refs[step_key] = (res.task_name, res.run_id)
                        if res.status == RunStatus.COMPLETE:
                            self._completed_by_step_key[step_key] = res
                        else:
                            failed_step_ids.add(id(step))

            if generation != self._run_generation:
                return

            wave_results = [
                results_by_pos[pos] for pos in sorted(results_by_pos)
            ]

            self._batches.append(wave_results)
            for res in wave_results:
                self.results[res.task_name][res.run_id] = res

            for _, _, _, handle in handles:
                for hist_res in handle.history:
                    self.results[hist_res.task_name][hist_res.run_id] = hist_res

            await self._save_workflow_checkpoint(workflow.status)
            self._notify_waiters()

            if failed is not None:
                raise OrchestralTaskFailure(
                    failed.error or f"Task '{failed.task_name}' failed",
                    failed.trace,
                    task_name=failed.task_name,
                    run_id=failed.run_id,
                )

            if not self._execute_next:
                break

        if generation == self._run_generation:
            self._finalize_pause_accounting()
            self.elapsed = time.monotonic() - self._start
            if not self._execute_next:
                workflow.status = WorkflowStatus.CANCELLED
            else:
                workflow.status = WorkflowStatus.COMPLETED
            await self._save_workflow_checkpoint(workflow.status)

    async def stop(self) -> None:
        self._execute_next = False
        self._stop_requested = True
        self._finalize_pause_accounting()

        if self._workflow.has_runner:
            await self._workflow.runner.stop()

        task = self._run_task
        if task is not None:
            if not task.done():
                task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            self._run_task = None

        if self._workflow.status in (WorkflowStatus.RUNNING, WorkflowStatus.PAUSED):
            self.elapsed = time.monotonic() - self._start
            self._workflow.status = WorkflowStatus.CANCELLED

        self._done = True
        self._notify_waiters()

    async def shutdown(self) -> None:
        await self.stop()

        if self._workflow.has_runner:
            await self._workflow.runner.shutdown()

        if self._owns_checkpoint_store and not self._store_closed:
            self._store_closed = True
            try:
                await _maybe_await(self._checkpoint_store.close())
            except Exception:
                pass

    def abort(self) -> None:
        self._run_generation += 1
        self._execute_next = False
        self._stop_requested = True
        self._finalize_pause_accounting()

        task = self._run_task
        if task is not None and not task.done():
            task.cancel()
        self._run_task = None

        if self._workflow.has_runner:
            self._workflow.runner.abort()

        if self._workflow.status in (WorkflowStatus.RUNNING, WorkflowStatus.PAUSED):
            self.elapsed = time.monotonic() - self._start
            self._workflow.status = WorkflowStatus.CANCELLED

        if self._owns_checkpoint_store and not self._store_closed:
            self._store_closed = True
            try:
                res = self._checkpoint_store.close()
                if inspect.iscoroutine(res):
                    res.close()
            except Exception:
                pass

        self._done = True
        self._notify_waiters()
