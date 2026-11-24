import asyncio
import time
import traceback
from collections import defaultdict
from typing import Any, AsyncIterator

from taskex import Env
from taskex.models import RunStatus, ShellProcess, TaskRun, TaskType

from .error import OrchestralTaskFailure
from .graph import ExecutionPlan
from .runner import RunHandle, StepResult
from .step import Step
from .step_type import StepType
from .util import parse_duration
from .workflow import Workflow
from .workflow_status import WorkflowStatus


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
            process_id=-1,
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
        result=None,
        task_type=TaskType.CALLABLE,
    )


class Orchestral:
    def __init__(
        self,
        config: Env | None = None,
    ) -> None:
        self._config = config
        self._workflow = Workflow(config=config)

        self._batches: list[list[StepResult]] = []
        self._batch_waiter = asyncio.Event()
        self._done = False
        self._run_task: asyncio.Task[None] | None = None
        self._run_generation = 0
        self._execute_next = False
        self._stop_requested = False

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
    def is_running(self) -> bool:
        return self._run_task is not None and not self._run_task.done()

    def execution_order(self) -> list[list[str]]:
        """Validate the active workflow's dependency graph and return wave-by-wave step names."""
        return self._workflow.execution_order()

    def _notify_waiters(self) -> None:
        waiter = self._batch_waiter
        self._batch_waiter = asyncio.Event()
        waiter.set()

    def _reset_run_state(self) -> None:
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

    def create(self) -> Workflow:
        if self.is_running:
            raise RuntimeError(
                "Cannot create a new workflow while a workflow is currently running"
            )

        if self._workflow.has_runner:
            self._workflow.runner.abort()

        self._run_task = None
        self._reset_run_state()
        self._workflow = Workflow(config=self._config)
        return self._workflow

    async def run(
        self,
        wait: bool = False,
        fail_fast: bool = False,
        timeout: str | int | float | None = None,
    ) -> None:
        if self.is_running:
            raise RuntimeError("Workflow is already running")

        timeout_seconds = parse_duration(timeout, name="timeout")
        workflow = self._workflow
        plan = workflow.build_plan()

        self._reset_run_state()
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
            )
        )
        self._run_task = task

        if wait:
            await self._await_run_task(task)

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
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
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
    ) -> None:
        try:
            if timeout is not None:
                await asyncio.wait_for(
                    self._run_workflow(
                        generation=generation,
                        workflow=workflow,
                        plan=plan,
                        fail_fast=fail_fast,
                    ),
                    timeout=timeout,
                )
            else:
                await self._run_workflow(
                    generation=generation,
                    workflow=workflow,
                    plan=plan,
                    fail_fast=fail_fast,
                )

        except asyncio.TimeoutError:
            if generation == self._run_generation:
                self.elapsed = time.monotonic() - self._start
                self.trace = traceback.format_exc()
                self.error = TimeoutError(
                    f"Err. - Workflow exceeded timeout of {timeout} seconds"
                )
                workflow.status = WorkflowStatus.FAILED

        except asyncio.CancelledError:
            if generation == self._run_generation:
                self.elapsed = time.monotonic() - self._start
                if workflow.status == WorkflowStatus.RUNNING:
                    workflow.status = WorkflowStatus.CANCELLED
            raise

        except OrchestralTaskFailure as err:
            if generation == self._run_generation:
                self.elapsed = time.monotonic() - self._start
                self.trace = err.trace or traceback.format_exc()
                self.error = err
                workflow.status = WorkflowStatus.FAILED

        except Exception as err:
            if generation == self._run_generation:
                self.elapsed = time.monotonic() - self._start
                self.trace = traceback.format_exc()
                self.error = err
                workflow.status = WorkflowStatus.FAILED

        finally:
            if generation == self._run_generation:
                self._done = True
                self._notify_waiters()

    async def _run_workflow(
        self,
        *,
        generation: int,
        workflow: Workflow,
        plan: ExecutionPlan,
        fail_fast: bool = False,
    ) -> None:
        failed_step_ids: set[int] = set()

        for wave in plan.waves:
            if not self._execute_next or generation != self._run_generation:
                break

            if not wave:
                continue

            runner = workflow.runner
            runnable: list[tuple[int, Step[Any]]] = []
            results_by_pos: dict[int, StepResult] = {}

            for pos, step in enumerate(wave):
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
                    runnable.append((pos, step))

            handles: list[tuple[int, Step[Any], RunHandle]] = []
            wave_tokens: list[str] = []
            failed: StepResult | None = None

            if runnable:
                try:
                    for pos, step in runnable:
                        handle = runner.run_step(step)
                        handles.append((pos, step, handle))
                        wave_tokens.append(handle.token)

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

                handle_by_run_id = {h.run_id: (pos, step, h) for pos, step, h in handles}
                # Map completed results back to their wave positions; for repeating steps,
                # the final result's run_id may differ from handle.run_id (iteration 0),
                # so also fall back to matching by history membership.
                for res in runnable_results:
                    entry = handle_by_run_id.get(res.run_id)
                    if entry is None:
                        for pos, step, h in handles:
                            if any(hr.run_id == res.run_id for hr in h.history):
                                entry = (pos, step, h)
                                break
                    if entry is not None:
                        pos, step, _ = entry
                        results_by_pos[pos] = res
                        if res.status != RunStatus.COMPLETE:
                            failed_step_ids.add(id(step))

            if generation != self._run_generation:
                return

            wave_results = [
                results_by_pos[pos] for pos in sorted(results_by_pos)
            ]

            self._batches.append(wave_results)
            for res in wave_results:
                self.results[res.task_name][res.run_id] = res

            for _, _, handle in handles:
                for hist_res in handle.history:
                    self.results[hist_res.task_name][hist_res.run_id] = hist_res

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
            self.elapsed = time.monotonic() - self._start
            if not self._execute_next:
                workflow.status = WorkflowStatus.CANCELLED
            else:
                workflow.status = WorkflowStatus.COMPLETED

    async def stop(self) -> None:
        self._execute_next = False
        self._stop_requested = True

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

        if self._workflow.status == WorkflowStatus.RUNNING:
            self.elapsed = time.monotonic() - self._start
            self._workflow.status = WorkflowStatus.CANCELLED

        self._done = True
        self._notify_waiters()

    async def shutdown(self) -> None:
        await self.stop()

        if self._workflow.has_runner:
            await self._workflow.runner.shutdown()

    def abort(self) -> None:
        self._run_generation += 1
        self._execute_next = False
        self._stop_requested = True

        task = self._run_task
        if task is not None and not task.done():
            task.cancel()
        self._run_task = None

        if self._workflow.has_runner:
            self._workflow.runner.abort()

        if self._workflow.status == WorkflowStatus.RUNNING:
            self.elapsed = time.monotonic() - self._start
            self._workflow.status = WorkflowStatus.CANCELLED

        self._done = True
        self._notify_waiters()
