# API Reference

All primary classes and enums are exported from the top-level `orchestral` package:

```python
from orchestral import (
    Group,
    Orchestral,
    OrchestralTaskFailure,
    RunHandle,
    Step,
    StepResult,
    StepType,
    Workflow,
    WorkflowRunner,
    WorkflowStatus,
    WorkflowStatusName,
)
```

---

## `Orchestral`

```python
class Orchestral:
    def __init__(self, config: Env | None = None) -> None
```

Main entry point for building, running, streaming, and tearing down workflows.

### Attributes & Properties

- **`results: dict[str, dict[int, StepResult]]`** — Nested mapping of `task_name -> {run_id: ShellProcess | TaskRun}` populated as groups complete. Reset at the start of each `run()` and on `create()`.
- **`error: Exception | None`** — Workflow-level error (`OrchestralTaskFailure`, `TimeoutError`, or unexpected `Exception`), or `None` if no workflow-level error occurred.
- **`trace: str | None`** — Formatted traceback associated with `error`, or `None`.
- **`elapsed: float`** — Total workflow execution duration in seconds.
- **`status -> WorkflowStatus`** *(property)* — Current status of the active `Workflow`.
- **`workflow -> Workflow`** *(property)* — The active `Workflow` instance.
- **`is_running -> bool`** *(property)* — `True` while a workflow execution task is active.

### Context Managers & Iteration

- **`with orch as workflow:` (`__enter__` / `__exit__`)** — Calls `create()` and returns a fresh `Workflow` configured with `config`. Does not shut down on exit so you can run the workflow immediately afterward.
- **`async with Orchestral(config=...) as orch:` (`__aenter__` / `__aexit__`)** — Returns `self` and automatically calls `await self.shutdown()` on exit.
- **`async for batch in orch:` (`__aiter__`)** — Yields `list[ShellProcess | TaskRun]` for each completed group in execution order and terminates when execution finishes or is stopped.

### Methods

- **`create() -> Workflow`**
  Creates and attaches a fresh `Workflow` using the configured `Env`, resetting run state and cleaning up any prior idle runner. Raises `RuntimeError` if a workflow is currently running.

- **`async run(wait: bool = False, fail_fast: bool = False, timeout: str | int | float | None = None) -> None`**
  Executes the active workflow.
  - `wait`: If `True`, awaits completion before returning. If `False`, runs in a background `asyncio.Task`.
  - `fail_fast`: If `True`, cancels remaining siblings in the active group and halts subsequent groups on the first `RunStatus.FAILED` step, setting `error = OrchestralTaskFailure(...)` and `status = WorkflowStatus.FAILED`.
  - `timeout`: Optional workflow-wide deadline in seconds or duration string (`"500ms"`, `"10s"`, `"2m"`, etc.).
  - Raises `RuntimeError` if already running; raises `ValueError`/`TypeError` on invalid `timeout`.

- **`async wait() -> None`**
  Awaits completion of an in-progress non-blocking (`wait=False`) run. Safe no-op if no run was started.

- **`async stop() -> None`**
  Gracefully cancels active steps (terminating and reaping subprocesses), halts subsequent groups, marks a running workflow `WorkflowStatus.CANCELLED`, and unblocks `async for` consumers. Idempotent.

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

Container of ordered `Group`s with a lazily initialized `WorkflowRunner`.

- **`group() -> Group`** — Creates, appends, and returns a new `Group` with a unique snowflake `id`.
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

A batch of `Step`s executed concurrently.

- **`id: int`** — Unique snowflake group identifier.
- **`steps -> list[Step[Any]]`** *(property)* — Shallow copy of steps in the group.
- Supports `len(group)` and `for step in group:`.

### `add_task`

```python
def add_task(
    self,
    step: WorkflowTask[T],
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

## `Step` & `StepType`

```python
class StepType(Enum):
    SHELL = "SHELL"
    SYNC = "SYNC"
    ASYNC = "ASYNC"
```

`Step` validates its callable/command and execution options eagerly at construction time:

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

## `OrchestralTaskFailure`

```python
class OrchestralTaskFailure(Exception):
    def __init__(
        self,
        error: str,
        trace: str | None = None,
        *,
        task_name: str | None = None,
        run_id: int | None = None,
    ) -> None
```

Assigned to `orch.error` when a step fails under `fail_fast=True`.

- `error: str` — Error message from the failed step.
- `trace: str | None` — Captured traceback (for Python callables) or `None`.
- `task_name: str | None` — Name/alias of the failing step.
- `run_id: int | None` — Unique snowflake ID of the failing run.

---

## Result & Configuration Models (from `taskex`)

`orchestral` returns standard `taskex` Pydantic models:

- **`taskex.Env`** — Configuration model (`MERCURY_SYNC_EXECUTOR_TYPE`: `"process" | "thread"`, `MERCURY_SYNC_TASK_RUNNER_MAX_THREADS`: `int`, etc.).
- **`taskex.models.ShellProcess`** — Result for shell steps (`run_id`, `task_name`, `process_id`, `command`, `args`, `status`, `return_code`, `env`, `working_directory`, `command_type`, `result` (stdout), `error` (stderr or failure message), `trace`, `start`, `end`, `elapsed`).
- **`taskex.models.TaskRun`** — Result for Python callable steps (`run_id`, `task_name`, `status`, `result`, `error`, `trace`, `start`, `end`, `elapsed`).
- **`taskex.models.RunStatus`** — Per-run status enum (`CREATED`, `PENDING`, `IDLE`, `RUNNING`, `COMPLETE`, `CANCELLED`, `FAILED`).
