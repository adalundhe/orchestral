import asyncio
import time
import traceback
from collections import defaultdict
from typing import AsyncIterator

from taskex import Env
from taskex.models import RunStatus

from .error import OrchestralTaskFailure
from .runner import RunHandle, StepResult
from .util import parse_duration
from .workflow import Workflow
from .workflow_status import WorkflowStatus


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

        self._reset_run_state()
        generation = self._run_generation
        workflow = self._workflow
        self._start = time.monotonic()
        self._execute_next = True
        workflow.status = WorkflowStatus.RUNNING

        task = asyncio.create_task(
            self._execute_workflow(
                generation=generation,
                workflow=workflow,
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
        fail_fast: bool = False,
        timeout: float | None = None,
    ) -> None:
        try:
            if timeout is not None:
                await asyncio.wait_for(
                    self._run_workflow(
                        generation=generation,
                        workflow=workflow,
                        fail_fast=fail_fast,
                    ),
                    timeout=timeout,
                )
            else:
                await self._run_workflow(
                    generation=generation,
                    workflow=workflow,
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
        fail_fast: bool = False,
    ) -> None:
        for group in workflow:
            if not self._execute_next or generation != self._run_generation:
                break

            steps = group.steps
            if not steps:
                continue

            runner = workflow.runner
            handles: list[RunHandle] = []
            group_tokens: list[str] = []
            try:
                for step in steps:
                    handle = runner.run_step(step)
                    handles.append(handle)
                    group_tokens.append(handle.token)

                if fail_fast:
                    group_results, failed = await runner.wait_all_fail_fast(
                        group_tokens
                    )
                else:
                    group_results = await runner.wait_all(group_tokens)
                    failed = None
            except BaseException:
                if group_tokens:
                    await runner.cancel_many(group_tokens)
                raise

            if generation != self._run_generation:
                return

            self._batches.append(group_results)
            for res in group_results:
                self.results[res.task_name][res.run_id] = res

            for handle in handles:
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
