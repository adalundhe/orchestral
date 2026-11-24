# API Reference

All primary classes, functions, exceptions, and enums are exported from the top-level `orchestral` package:

```python
from orchestral import (
    ExecutionPlan,
    Group,
    Orchestral,
    OrchestralCycleError,
    OrchestralError,
    OrchestralGraphError,
    OrchestralTaskFailure,
    RunHandle,
    Step,
    StepResult,
    StepType,
    Workflow,
    WorkflowRunner,
    WorkflowStatus,
    WorkflowStatusName,
    build_execution_plan,
)
```

---

## `Orchestral`

```python
class Orchestral:
    def __init__(self, config: Env | None = None) -> None
```

Main entry point for building, planning, running, streaming, and tearing down workflows.

### Attributes & Properties

- **`results: dict[str, dict[int, StepResult]]`** — Nested mapping of `task_name -> {run_id: ShellProcess | TaskRun}` populated as waves complete. Reset at the start of each `run()` and on `create()`.
- **`error: Exception | None`** — Workflow-level error (`OrchestralTaskFailure`, `TimeoutError`, or unexpected `Exception`), or `None` if no workflow-level error occurred.
- **`trace: str | None`** — Formatted traceback associated with `error`, or `None`.
- **`elapsed: float`** — Total workflow execution duration in seconds.
- **`status -> WorkflowStatus`** *(property)* — Current status of the active `Workflow`.
- **`workflow -> Workflow`** *(property)* — The active `Workflow` instance.
- **`is_running -> bool`** *(property)* — `True` while a workflow execution task is active.

### Context Managers & Iteration

- **`with orch as workflow:` (`__enter__` / `__exit__`)** — Calls `create()` and returns a fresh `Workflow` configured with `config`. Does not shut down on exit so you can run the workflow immediately afterward.
- **`async with Orchestral(config=...) as orch:` (`__aenter__` / `__aexit__`)** — Returns `self` and automatically calls `await self.shutdown()` on exit.
- **`async for batch in orch:` (`__aiter__`)** — Yields `list[ShellProcess | TaskRun]` for each completed wave in execution order and terminates when execution finishes or is stopped.

### Methods

- **`create() -> Workflow`**
  Creates and attaches a fresh `Workflow` using the configured `Env`, resetting run state and cleaning up any prior idle runner. Raises `RuntimeError` if a workflow is currently running.

- **`execution_order() -> list[list[str]]`**
  Validates the active workflow's dependency graph and returns the wave-by-wave step names. Raises `OrchestralGraphError` / `OrchestralCycleError` if invalid.

- **`async run(wait: bool = False, fail_fast: bool = False, timeout: str | int | float | None = None) -> None`**
  Validates the dependency graph and executes the active workflow wave by wave.
  - `wait`: If `True`, awaits completion before returning. If `False`, runs in a background `asyncio.Task`.
  - `fail_fast`: If `True`, cancels remaining siblings in the active wave and halts subsequent waves on the first `RunStatus.FAILED` step, setting `error = OrchestralTaskFailure(...)` and `status = WorkflowStatus.FAILED`.
  - `timeout`: Optional workflow-wide deadline in seconds or duration string (`"500ms"`, `"10s"`, `"2m"`, etc.).
  - Raises `RuntimeError` if already running; raises `ValueError`/`TypeError` on invalid `timeout`; raises `OrchestralGraphError`/`OrchestralCycleError` on invalid dependency graphs before launching any step.

- **`async wait() -> None`**
  Awaits completion of an in-progress non-blocking (`wait=False`) run. Safe no-op if no run was started.

- **`async stop() -> None`**
  Gracefully cancels active steps (terminating and reaping subprocesses), halts subsequent waves, marks a running workflow `WorkflowStatus.CANCELLED`, and unblocks `async for` consumers. Idempotent.

- **`async shutdown() -> None`**
  Calls `stop()` and shuts down the runner's executor cleanly. Preserves `COMPLETED`/`FAILED` status if already finished. Idempotent.

- **`abort() -> None`**
  Synchronous immediate teardown. Force-kills active subprocess groups (`SIGKILL`), cancels pending asyncio tasks and executor futures, shuts down the executor, marks a running workflow `WorkflowStatus.CANCELLED`, and unblocks `async for` consumers. Idempotent.

---

## `Workflow`

```python
class Workflow:
    def __init__(self, config: Env | None = None) -> None
```

Container of ordered `Group`s (and direct DAG steps) with a lazily initialized `WorkflowRunner`.

- **`group() -> Group`** — Creates, appends, and returns a new explicit sequential `Group` with a unique snowflake `id`.
- **`add_task(step, *args, alias=None, dependencies=None, ...) -> Step[T]`** — Adds a Python callable directly to the workflow DAG.
- **`add_command(step, *args, alias=None, dependencies=None, ...) -> Step[str]`** — Adds a shell/subprocess command directly to the workflow DAG.
- **`build_plan() -> ExecutionPlan`** — Validates the full workflow dependency graph and returns the `ExecutionPlan`.
- **`build_waves() -> list[list[Step[Any]]]`** — Validates the graph and returns ordered concurrent waves of `Step` instances.
- **`execution_order() -> list[list[str]]`** — Validates the graph and returns wave-by-wave step names.
- **`groups -> list[Group]`** *(property)* — Shallow copy of registered groups.
- **`runner -> WorkflowRunner`** *(property)* — Returns the workflow's `WorkflowRunner`, creating it on first access.
- **`has_runner -> bool`** *(property)* — `True` if `runner` has been instantiated.
- **`status: WorkflowStatus`** — Current workflow status.
- Supports `len(workflow)` and `for group in workflow:`.

---

## `Group`

```python
class Group:
    def __init__(self, group_id: int) -> None
```

A stage of `Step`s executed concurrently or in intra-group dependency waves.

- **`id: int`** — Unique snowflake group identifier.
- **`steps -> list[Step[Any]]`** *(property)* — Shallow copy of steps in the group.
- **`build_waves() -> list[list[Step[Any]]]`** — Validates intra-group dependencies and returns concurrent waves.
- **`execution_order() -> list[list[str]]`** — Returns wave-by-wave step names for this group.
- Supports `len(group)` and `for step in group:`.

### `add_task`

```python
def add_task(
    self,
    step: WorkflowTask[T],
    *args: Any,
    alias: str | None = None,
    dependencies: DependenciesInput = None,
    timeout: str | int | float | None = None,
    schedule: str | int | float | None = None,
    trigger: Literal["MANUAL", "ON_START"] = "MANUAL",
    repeat: int | Literal["NEVER", "ALWAYS"] = "NEVER",
    keep: int | None = None,
    max_age: str | int | float | None = None,
    keep_policy: Literal["COUNT", "AGE", "COUNT_AND_AGE"] = "COUNT",
    **kwargs: Any,
) -> Step[T]
```

Adds an `async` or `sync` Python callable step and returns the created `Step`.

### `add_command`

```python
def add_command(
    self,
    step: str,
    *args: str,
    alias: str | None = None,
    dependencies: DependenciesInput = None,
    env: dict[str, Any] | None = None,
    cwd: str | None = None,
    shell: bool = False,
    timeout: str | int | float | None = None,
    schedule: str | int | float | None = None,
    trigger: Literal["MANUAL", "ON_START"] = "MANUAL",
    repeat: int | Literal["NEVER", "ALWAYS"] = "NEVER",
    keep: int | None = None,
    max_age: str | int | float | None = None,
    keep_policy: Literal["COUNT", "AGE", "COUNT_AND_AGE"] = "COUNT",
) -> Step[str]
```

Adds a shell/subprocess command step and returns the created `Step`.

---

## `ExecutionPlan` & `build_execution_plan`

```python
@dataclass(frozen=True)
class ExecutionPlan:
    waves: list[list[Step[Any]]]
    upstream_step_ids: dict[int, tuple[int, ...]]
    step_by_id: dict[int, Step[Any]]

    def execution_order(self) -> list[list[str]]: ...

def build_execution_plan(groups: Sequence[Sequence[Step[Any]]]) -> ExecutionPlan: ...
```

Compiles one or more ordered groups of `Step`s into validated concurrent execution waves using Kahn's topological sort.

---

## `Step` & `StepType`

```python
class StepType(Enum):
    SHELL = "SHELL"
    SYNC = "SYNC"
    ASYNC = "ASYNC"
```

`Step` validates its callable/command, dependencies, and execution options eagerly at construction time:

- Accepts dependencies via positional `*deps` and/or keyword `dependencies=` (single `Step`, `str`, callable, or any iterable thereof; deduplicated in order).
- Rejects empty command strings, non-callables, and already-instantiated coroutine objects.
- Validates `timeout`, `schedule`, `max_age`, `repeat`, `keep`, `trigger`, `keep_policy`, and `alias`.
- Exposes `call`, `args`, `kwargs`, `shell_args`, `dependencies`, `name`, `alias`, `type`, `timeout`, `timeout_seconds`, `schedule`, `schedule_seconds`, `trigger`, `repeat`, `keep`, `max_age`, `max_age_seconds`, `keep_policy`, `env`, `cwd`, `shell`.

---

## `WorkflowStatus`

```python
class WorkflowStatus(Enum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    READY = "READY"
    RUNNING = "RUNNING"
    CANCELLED = "CANCELLED"
```

---

## Exceptions

- **`OrchestralError(Exception)`** — Base class for orchestral exceptions.
- **`OrchestralGraphError(OrchestralError, ValueError)`** — Invalid dependency graph (missing step, external step not in workflow, or dependency pointing to a later group).
- **`OrchestralCycleError(OrchestralGraphError)`** — Dependency cycle detected. Exposes `cycle: list[str]`.
- **`OrchestralTaskFailure(OrchestralError)`** — Assigned to `orch.error` when a step fails under `fail_fast=True`. Exposes `error: str`, `trace: str | None`, `task_name: str | None`, and `run_id: int | None`.

---

## Result & Configuration Models (from `taskex`)

`orchestral` returns standard `taskex` Pydantic models:

- **`taskex.Env`** — Configuration model (`MERCURY_SYNC_EXECUTOR_TYPE`: `"process" | "thread"`, `MERCURY_SYNC_TASK_RUNNER_MAX_THREADS`: `int`, etc.).
- **`taskex.models.ShellProcess`** — Result for shell steps (`run_id`, `task_name`, `process_id`, `command`, `args`, `status`, `return_code`, `env`, `working_directory`, `command_type`, `result` (stdout), `error` (stderr or failure message), `trace`, `start`, `end`, `elapsed`).
- **`taskex.models.TaskRun`** — Result for Python callable steps (`run_id`, `task_name`, `status`, `result`, `error`, `trace`, `start`, `end`, `elapsed`).
- **`taskex.models.RunStatus`** — Per-run status enum (`CREATED`, `PENDING`, `IDLE`, `RUNNING`, `COMPLETE`, `CANCELLED`, `FAILED`).
