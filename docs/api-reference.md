# API Reference

All primary classes, functions, exceptions, enums, and `taskex` checkpointing/pause helpers are exported from the top-level `orchestral` package:

```python
from orchestral import (
    Checkpoint,
    CheckpointStore,
    Env,
    ExecutionPlan,
    Group,
    MemoryCheckpointStore,
    Orchestral,
    OrchestralCycleError,
    OrchestralError,
    OrchestralGraphError,
    OrchestralTaskFailure,
    RunContext,
    RunHandle,
    RunStatus,
    SQLiteCheckpointStore,
    ShellProcess,
    Step,
    StepResult,
    StepType,
    TaskRun,
    Workflow,
    WorkflowRunner,
    WorkflowStatus,
    WorkflowStatusName,
    build_execution_plan,
    checkpoint,
    checkpoint_sync,
    get_run_context,
    last_checkpoint,
    pause_point,
    pause_point_sync,
)
```

---

## `Orchestral`

```python
class Orchestral:
    def __init__(
        self,
        config: Env | None = None,
        checkpoint_store: CheckpointStore | None = None,
        workflow_id: str | None = None,
    ) -> None
```

Main entry point for building, planning, running, pausing, resuming, checkpointing, streaming, and tearing down workflows.

### Attributes & Properties

- **`results: dict[str, dict[int, StepResult]]`** — Nested mapping of `task_name -> {run_id: ShellProcess | TaskRun}` populated as waves complete. Reset at the start of each `run()` and on `create()`.
- **`error: Exception | None`** — Workflow-level error (`OrchestralTaskFailure`, `TimeoutError`, or unexpected `Exception`), or `None` if no workflow-level error occurred.
- **`trace: str | None`** — Formatted traceback associated with `error`, or `None`.
- **`elapsed: float`** — Total workflow execution duration in seconds.
- **`paused_duration -> float`** *(property)* — Cumulative seconds spent in `WorkflowStatus.PAUSED`.
- **`status -> WorkflowStatus`** *(property)* — Current status of the active `Workflow`.
- **`workflow -> Workflow`** *(property)* — The active `Workflow` instance.
- **`workflow_id -> str`** *(property)* — Logical workflow identifier used for durable workflow checkpoints (defaults to `"default"`).
- **`checkpoint_store -> CheckpointStore`** *(property)* — Active checkpoint store (`MemoryCheckpointStore`, `SQLiteCheckpointStore`, or user-supplied store).
- **`is_running -> bool`** *(property)* — `True` while a workflow execution task is active (including when paused).
- **`is_paused -> bool`** *(property)* — `True` when `status == WorkflowStatus.PAUSED`.
- **`active_tokens -> list[str]`** *(property)* — Run tokens currently active in the in-flight wave.

### Context Managers & Iteration

- **`with orch as workflow:` (`__enter__` / `__exit__`)** — Calls `create()` and returns a fresh `Workflow` configured with `config` and `checkpoint_store`. Does not shut down on exit.
- **`async with Orchestral(...) as orch:` (`__aenter__` / `__aexit__`)** — Returns `self` and automatically calls `await self.shutdown()` on exit.
- **`async for batch in orch:` (`__aiter__`)** — Yields `list[ShellProcess | TaskRun]` for each completed wave in execution order and terminates when execution finishes or is stopped.

### Execution, Pause & Resume Methods

- **`create() -> Workflow`**
  Creates and attaches a fresh `Workflow` using `config` and `checkpoint_store`, resetting run state and cleaning up any prior idle runner. Raises `RuntimeError` if a workflow is currently running.

- **`execution_order() -> list[list[str]]`**
  Validates the active workflow's dependency graph and returns the wave-by-wave step names.

- **`async run(wait: bool = False, fail_fast: bool = False, timeout: str | int | float | None = None) -> None`**
  Validates the dependency graph and executes the active workflow wave by wave from scratch.
  - `timeout` is pause-aware: countdown freezes while `PAUSED`.
  - Raises `RuntimeError` if already running; `ValueError`/`TypeError` on invalid `timeout`; `OrchestralGraphError`/`OrchestralCycleError` on invalid graphs before launching any step.

- **`async pause() -> bool`**
  Suspends a running workflow: transitions status to `WorkflowStatus.PAUSED`, freezes workflow timeout, pauses all active runs/schedules, prevents subsequent waves from launching, and saves a workflow checkpoint. Returns `True` if paused (or already paused), `False` otherwise.

- **`async resume(wait: bool = False, fail_fast: bool = False, timeout: str | int | float | None = None, *, rerun_completed: bool = False) -> None`**
  - If currently `WorkflowStatus.PAUSED`: live-resumes all paused steps and continues executing remaining waves (awaiting completion when `wait=True`).
  - Otherwise (after failure, cancellation, or in a new `Orchestral` instance backed by a shared `CheckpointStore`): loads the latest workflow checkpoint for `workflow_id`, restores and skips already-completed steps (unless `rerun_completed=True`), and launches remaining steps seeded with their latest task checkpoints.

- **`async pause_step(target: str | Step[Any]) -> bool`** — Pauses one active step by run token, step name/alias, or `Step`.
- **`async resume_step(target: str | Step[Any]) -> bool`** — Live-resumes one paused step by run token, step name/alias, or `Step`.

- **`async wait() -> None`** — Awaits completion of an in-progress non-blocking (`wait=False`) run.
- **`async stop() -> None`** — Gracefully cancels active/paused steps and halts subsequent waves (`WorkflowStatus.CANCELLED`). Idempotent.
- **`async shutdown() -> None`** — Calls `stop()`, shuts down the runner/executor cleanly, and closes an owned `CheckpointStore`. Preserves `COMPLETED`/`FAILED` status if already finished. Idempotent.
- **`abort() -> None`** — Synchronous immediate teardown (`SIGKILL` active subprocess groups, cancel tasks/futures, shut down executor, close owned store). Idempotent.

### Checkpoint Methods

- **`async checkpoint(target: str | Step[Any], state: Any, *, metadata: dict[str, Any] | None = None) -> Checkpoint`** — Saves an external checkpoint for an active run (by token, step name/alias, or `Step`).
- **`async get_checkpoint(target: str | Step[Any]) -> Checkpoint | None`** — Retrieves the latest checkpoint for a token, step name/alias, or `Step`.
- **`async list_checkpoints(token: str) -> list[Checkpoint]`** — Returns all checkpoints for a run token in sequence order.
- **`async clear_checkpoints(target: str | Step[Any] | None = None) -> None`** — Clears checkpoints for a token, a step, or (when `None`) the entire workflow.

---

## `Workflow`

```python
class Workflow:
    def __init__(
        self,
        config: Env | None = None,
        checkpoint_store: CheckpointStore | None = None,
    ) -> None
```

Container of ordered `Group`s (and direct DAG steps) with a lazily initialized `WorkflowRunner`.

- **`group() -> Group`** — Creates, appends, and returns a new explicit sequential `Group`.
- **`add_task(step, *args, alias=None, dependencies=None, resume_from=None, ...) -> Step[T]`** — Adds a Python callable directly to the workflow DAG.
- **`add_command(step, *args, alias=None, dependencies=None, resume_from=None, ...) -> Step[str]`** — Adds a shell/subprocess command directly to the workflow DAG.
- **`build_plan() -> ExecutionPlan`** / **`build_waves() -> list[list[Step[Any]]]`** / **`execution_order() -> list[list[str]]`** — Validate and inspect execution waves.
- **`groups -> list[Group]`**, **`runner -> WorkflowRunner`**, **`has_runner -> bool`**, **`status: WorkflowStatus`**.

---

## `Group`

```python
class Group:
    def __init__(self, group_id: int) -> None
```

- **`add_task(step, *args, alias=None, dependencies=None, timeout=None, schedule=None, trigger="MANUAL", repeat="NEVER", keep=None, max_age=None, keep_policy="COUNT", resume_from=None, **kwargs) -> Step[T]`**
- **`add_command(step, *args, alias=None, dependencies=None, env=None, cwd=None, shell=False, timeout=None, schedule=None, trigger="MANUAL", repeat="NEVER", keep=None, max_age=None, keep_policy="COUNT", resume_from=None) -> Step[str]`**
- **`build_waves() -> list[list[Step[Any]]]`**, **`execution_order() -> list[list[str]]`**, **`steps -> list[Step[Any]]`**, **`id: int`**.

---

## `Step` & `StepType`

```python
class StepType(Enum):
    SHELL = "SHELL"
    SYNC = "SYNC"
    ASYNC = "ASYNC"
```

Validates callable/command, `dependencies`, `resume_from` (`str | Checkpoint | None`), `timeout`, `schedule`, `max_age`, `repeat`, `keep`, `trigger`, `keep_policy`, and `alias` eagerly at construction time.

---

## `WorkflowStatus`

```python
class WorkflowStatus(Enum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"
```

---

## Exceptions

- **`OrchestralError(Exception)`** — Base class for orchestral exceptions.
- **`OrchestralGraphError(OrchestralError, ValueError)`** — Invalid dependency graph (missing step, external step not in workflow, or forward dependency into a later group).
- **`OrchestralCycleError(OrchestralGraphError)`** — Dependency cycle detected; exposes `cycle: list[str]`.
- **`OrchestralTaskFailure(OrchestralError)`** — Assigned to `orch.error` when a step fails under `fail_fast=True`; exposes `error`, `trace`, `task_name`, `run_id`.

---

## Re-Exported `taskex` Checkpointing, Pause & Result Types

- **Stores**: `CheckpointStore`, `MemoryCheckpointStore`, `SQLiteCheckpointStore`
- **In-task helpers**: `RunContext`, `get_run_context()`, `last_checkpoint()`, `checkpoint()`, `checkpoint_sync()`, `pause_point()`, `pause_point_sync()`
- **Models & Config**: `Env` (including `MERCURY_SYNC_EXECUTOR_TYPE: "process" | "thread" | "none"` and `MERCURY_SYNC_CHECKPOINT_DB_PATH`), `Checkpoint`, `ShellProcess` (with `.checkpoint`, `.paused_duration`), `TaskRun` (with `.checkpoint`, `.paused_duration`), `RunStatus` (including `RunStatus.PAUSED`).
