# Execution Model & Configuration

## Groups and Concurrency

A `Workflow` is an ordered list of `Group`s:

```
Group 1: [ step A, step B, step C ]   <-- launched together, run concurrently
                    |
                    v  (all steps in Group 1 complete)
Group 2: [ step D, step E ]           <-- launched together, run concurrently
                    |
                    v
Group 3: [ step F ]
```

- Empty groups (groups with no steps added) are skipped automatically.
- All steps in a group are launched together via `WorkflowRunner` and awaited concurrently.
- Each group's completed results are recorded in `orch.results` and emitted to any `async for batch in orch:` consumers before the next group begins.

## Step Types

`Step` classifies each step automatically into a `StepType`:

| `StepType`       | Input to `Group`                   | Execution Mechanism                                                     | Result Model   |
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
group.add_task(
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
