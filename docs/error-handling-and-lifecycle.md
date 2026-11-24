# Error Handling, Timeouts & Lifecycle

## Workflow Status State Machine

`orch.status` returns a `WorkflowStatus` enum value reflecting the current workflow state:

| Status                     | Meaning                                                                             |
| -------------------------- | ----------------------------------------------------------------------------------- |
| `WorkflowStatus.READY`     | Workflow has been created and has not started running yet.                          |
| `WorkflowStatus.RUNNING`   | Workflow is currently executing.                                                    |
| `WorkflowStatus.COMPLETED` | All groups finished without a workflow-level failure, timeout, or cancellation.     |
| `WorkflowStatus.FAILED`    | Workflow failed due to `fail_fast=True`, a workflow timeout, or an internal error.  |
| `WorkflowStatus.CANCELLED` | Workflow was cancelled mid-run via `stop()`, `shutdown()`, `abort()`, or task cancel.|

```
READY ──▶ RUNNING ──┬──▶ COMPLETED
                    ├──▶ FAILED     (fail_fast task failure, workflow timeout, exception)
                    └──▶ CANCELLED  (stop, shutdown, abort, or outer task cancellation)
```

Calling `shutdown()` or `abort()` **after** a workflow has already reached `COMPLETED` or `FAILED` preserves that terminal status.

## Error Handling & `fail_fast`

### `fail_fast=False` (Default)

- Task exceptions, non-zero shell exit codes, and per-step timeouts are captured into each step's `TaskRun` or `ShellProcess` (`status == RunStatus.FAILED`, `error` populated, `trace` populated for Python exceptions).
- All remaining steps in the group and all subsequent groups still execute.
- `orch.error` remains `None` and `orch.status` becomes `WorkflowStatus.COMPLETED`.

### `fail_fast=True`

- Works identically in both `wait=True` and `wait=False` modes.
- Triggers when any step finishes with `status == RunStatus.FAILED` (a shell command that exits `0` while writing warnings to `stderr` has `RunStatus.COMPLETE` and does **not** falsely trigger `fail_fast`).
- Immediately cancels and reaps any still-running sibling steps in the same group.
- Records all completed results from the failing group into `orch.results` and emits that batch to `async for` consumers.
- Skips all subsequent groups.
- Sets `orch.status = WorkflowStatus.FAILED`, `orch.error = OrchestralTaskFailure(...)`, and `orch.trace` to the failing task's traceback (when available).

```python
from orchestral import Orchestral, OrchestralTaskFailure, WorkflowStatus

async with Orchestral() as orch:
    with orch as workflow:
        g1 = workflow.group()
        g1.add_command("false", alias="failing_cmd")

        g2 = workflow.group()
        g2.add_command("echo", "never runs", alias="skipped")

    await orch.run(wait=True, fail_fast=True)

    assert orch.status == WorkflowStatus.FAILED
    assert isinstance(orch.error, OrchestralTaskFailure)
    print("Failed task:", orch.error.task_name, orch.error.run_id)
    print("Error:", orch.error)
```

## Timeouts & Duration Strings

Both step-level (`timeout=` on `add_task` / `add_command`) and workflow-level (`timeout=` on `orch.run(...)`) deadlines accept:

- `None` — no timeout.
- `int` or `float` — non-negative finite seconds (e.g., `0.5`, `30`, `0`).
- `str` — one or more `<number><unit>` tokens using `ms`, `s`, `m`, `h`, `d`, `w` (or bare seconds):
  - `"500ms"` → `0.5`
  - `"1.5s"` → `1.5`
  - `"2m30s"` → `150.0`
  - `"1h 15m"` → `4500.0`

Invalid duration strings (such as `""`, `"abc"`, `"-5s"`) or negative numbers raise `ValueError` immediately.

### Step Timeout vs. Workflow Timeout

- **Step timeout**: Cancels only that step (terminating and reaping its subprocess if shell), marks that step's result as `RunStatus.FAILED` with a timeout error message, and obeys `fail_fast`.
- **Workflow timeout**: Cancels all currently running steps/subprocesses across the active group, sets `orch.status = WorkflowStatus.FAILED`, and sets `orch.error = TimeoutError(...)`.

## Streaming Batch Results (`async for`)

Iterate over `orch` with `async for` to receive each group's results (`list[ShellProcess | TaskRun]`) as soon as that group completes:

```python
async with Orchestral() as orch:
    with orch as workflow:
        workflow.group().add_command("echo", "first", alias="first")
        workflow.group().add_command("echo", "second", alias="second")

    await orch.run(wait=False)
    async for batch in orch:
        for res in batch:
            print(res.task_name, "->", res.result.strip())
```

Guarantees:

- **Ordered & lossless**: Every completed group's batch is preserved in order; fast consecutive groups never overwrite earlier batches.
- **Multiple consumers**: Multiple concurrent `async for` loops on the same `Orchestral` instance each see the complete sequence of batches.
- **Replay after completion**: Iterating after `await orch.run(wait=True)` yields all recorded batches and finishes immediately.
- **Always terminates**: Iteration ends cleanly on empty workflows, failures, timeouts, `stop()`, `shutdown()`, and `abort()` without deadlocking.

## Lifecycle: `stop()`, `shutdown()`, `abort()`, and Re-Running

| Method / Pattern               | Sync / Async | Cancels active runs | Closes executor | Typical Use Case                                             |
| ------------------------------ | ------------ | ------------------- | --------------- | ------------------------------------------------------------ |
| `await orch.stop()`            | `async`      | Gracefully (SIGTERM → SIGKILL) | No   | Stop the current run while keeping the workflow inspectable/re-runnable |
| `await orch.shutdown()`        | `async`      | Gracefully (SIGTERM → SIGKILL) | Yes  | Standard teardown after running                              |
| `orch.abort()`                 | `sync`       | Immediately (SIGKILL)          | Yes  | Non-async emergency teardown / signal callbacks              |
| `async with Orchestral() as o` | `async`      | Calls `await o.shutdown()` on exit | Yes | Recommended default to guarantee leak-free cleanup          |

All teardown methods are **idempotent** and safe to call before `run()`, after completion, or multiple times.

### Re-running and Concurrency Guards

- Calling `await orch.run(...)` while a workflow is already running raises `RuntimeError`.
- Calling `orch.create()` (or entering `with orch as workflow:`) while a workflow is running raises `RuntimeError`.
- Re-running the same workflow after it finishes (or creating a new workflow) cleanly resets `results`, `error`, `trace`, `elapsed`, and streaming batches.
- Creating a new workflow automatically shuts down the previous workflow's runner if one was initialized.
