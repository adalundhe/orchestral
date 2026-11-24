# Execution Model & Configuration

## Dependency-Graph Waves and Group Barriers

Before running a workflow, `orchestral` validates all declared dependencies and compiles the workflow into an `ExecutionPlan` of ordered **waves** using Kahn's level-by-level topological sort (`build_execution_plan`):

```
          ┌──▶ test_unit ──┐
build ────┤                ├──▶ deploy
          └──▶ test_int  ──┘

Wave 1: [ build ]                   <-- runs first
Wave 2: [ test_unit, test_int ]     <-- run concurrently once Wave 1 finishes
Wave 3: [ deploy ]                  <-- runs once Wave 2 finishes
```

### Rules of Wave Resolution

- **Within a group (or flat `workflow.add_task` / `add_command` DAG)**:
  - Steps with no unsatisfied dependencies start in Wave 1 and execute concurrently.
  - Each subsequent wave contains every step whose dependencies all completed in earlier waves, preserving step declaration order within each wave.
  - Steps may be declared in any order (a dependent step can be added before its dependency).
  - Duplicate step names work as a fan-in when referenced by name/callable (`dependencies=[fetch]` waits for every `fetch` step), or you can depend on a single specific step by passing the `Step` object returned by `add_task` / `add_command`.
- **Across explicit `Group`s**:
  - `workflow.group()` creates a strict sequential stage barrier: all waves in Group `k` finish before Group `k + 1` begins.
  - A step in Group `k` may depend on steps in the same group `k` or in any earlier group `< k`.
  - Depending on a step defined only in a later group `> k` raises `OrchestralGraphError`.
- **Upfront validation**:
  - Call `workflow.execution_order()`, `workflow.build_waves()`, or `orch.execution_order()` to validate and inspect waves before execution.
  - `await orch.run(...)` always validates the full workflow graph before launching a single step—cycles (`OrchestralCycleError`) and missing dependencies (`OrchestralGraphError`) fail immediately with zero partial execution.
  - Empty groups are skipped automatically.

## Step Types

`Step` classifies each step automatically into a `StepType`:

| `StepType`       | Input to `Workflow` / `Group`      | Execution Mechanism                                                     | Result Model   |
| ---------------- | ---------------------------------- | ----------------------------------------------------------------------- | -------------- |
| `StepType.SHELL` | `add_command(cmd: str, *args)`     | `asyncio.create_subprocess_exec` or `create_subprocess_shell`           | `ShellProcess` |
| `StepType.ASYNC` | `add_task(async_fn, *args, **kw)`  | Awaited directly on the running asyncio event loop                      | `TaskRun`      |
| `StepType.SYNC`  | `add_task(sync_fn, *args, **kw)`   | Submitted to `ProcessPoolExecutor` or `ThreadPoolExecutor` + semaphore  | `TaskRun`      |

### Shell Execution Details

- **No pipe deadlocks**: Output is collected with `process.communicate()`, so commands that emit large amounts of `stdout` or `stderr` (>64 KB) will not block on OS pipe buffers.
- **Safe decoding**: `stdout` and `stderr` are decoded as UTF-8 with `errors="replace"` so binary output never crashes the runner.
- **Exit codes and `stderr`**:
  - Exit code `0`: `status == RunStatus.COMPLETE`, `result == stdout`, and `error == stderr` (if non-empty) or `None`.
  - Non-zero exit code: `status == RunStatus.FAILED`, `return_code == <exit code>`, and `error` contains the captured `stderr`.
  - Spawn errors (e.g., command not found): `status == RunStatus.FAILED`, `process_id == -1`, `return_code == None`, with error details and traceback captured on the `ShellProcess`.
- **Process group isolation (POSIX)**: Subprocesses are spawned with `start_new_session=True`. On timeout, cancellation, `stop()`, `shutdown()`, or `abort()`, signals are sent to the entire process group so grandchild processes spawned by `shell=True` commands are cleaned up too.

### Sync Executor Configuration (`taskex.Env`)

Configure how synchronous callables execute by passing a `taskex.Env` instance to `Orchestral(config=...)`:

```python
from taskex import Env
from orchestral import Orchestral

config = Env(
    MERCURY_SYNC_EXECUTOR_TYPE="thread",      # "process" (default) or "thread"
    MERCURY_SYNC_TASK_RUNNER_MAX_THREADS=4,   # max worker count (default: os.cpu_count())
)

orch = Orchestral(config=config)
```

Key behaviors:

- **Lazy initialization**: The executor is created only when a `StepType.SYNC` task actually runs. Pure shell or async workflows allocate zero worker threads or processes.
- **Bounded concurrency**: Sync submissions are gated by an `asyncio.Semaphore(max_workers)` acquired with `async with`, guaranteeing permits are always released even if tasks fail, time out, or are cancelled.
- **Config preservation**: `config` is preserved whenever you call `orch.create()` or re-enter `with orch as workflow:`.
- **Pickling note**: `"process"` mode uses `concurrent.futures.ProcessPoolExecutor`, which requires top-level picklable functions and picklable arguments (especially on macOS/Windows using the `spawn` start method). Use `"thread"` mode for lambdas, local closures, bound methods on unpicklable objects, or I/O-bound synchronous functions.

## Scheduled & Repeating Steps

Individual steps support repetition with `schedule`, `repeat`, and history retention controls:

```python
workflow.add_task(
    poll_health,
    alias="health_check",
    schedule="500ms",        # delay between iterations
    repeat=5,                # "NEVER" (default), positive int, or "ALWAYS"
    keep=3,                  # max retained runs for COUNT / COUNT_AND_AGE
    max_age="2s",            # max age for AGE / COUNT_AND_AGE
    keep_policy="COUNT",     # "COUNT" | "AGE" | "COUNT_AND_AGE"
)
```

- `repeat="NEVER"` (default): runs once.
- `repeat=N` (positive `int`): runs `N` times with `schedule` delay between runs (no trailing sleep after iteration `N`). Retained results according to `keep_policy` are stored in `orch.results[task_name]`.
- `repeat="ALWAYS"`: requires `schedule` and runs until cancelled by a workflow timeout, `stop()`, `shutdown()`, or `abort()`. History is pruned on every iteration according to `keep_policy` so memory stays bounded.
