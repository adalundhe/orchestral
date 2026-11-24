# Error Handling, Timeouts & Lifecycle

## Graph Validation Errors

Before any step executes, `orch.run()` (and `workflow.build_waves()` / `execution_order()`) validates the dependency graph:

- **`OrchestralGraphError`** *(subclass of `OrchestralError` and `ValueError`)*: raised when a step references an unknown dependency name, a `Step` not belonging to the workflow, or a step located in a later explicit `Group`.
- **`OrchestralCycleError`** *(subclass of `OrchestralGraphError`)*: raised when a self-dependency or multi-step cycle exists. Inspect `.cycle` for the ordered list of step names forming the cycle (e.g., `["a", "b", "c", "a"]`).

Graph validation errors are raised directly before the workflow transitions out of `WorkflowStatus.READY`, so no steps execute partially.

## Workflow Status State Machine

`orch.status` returns a `WorkflowStatus` enum value reflecting the current workflow state:

| Status                     | Meaning                                                                             |
| -------------------------- | ----------------------------------------------------------------------------------- |
| `WorkflowStatus.READY`     | Workflow has been created and has not started running yet.                          |
| `WorkflowStatus.RUNNING`   | Workflow is currently executing.                                                    |
| `WorkflowStatus.COMPLETED` | All waves finished without a workflow-level failure, timeout, or cancellation.      |
| `WorkflowStatus.FAILED`    | Workflow failed due to `fail_fast=True`, a workflow timeout, or an internal error.  |
| `WorkflowStatus.CANCELLED` | Workflow was cancelled mid-run via `stop()`, `shutdown()`, `abort()`, or task cancel.|

```
READY ──▶ RUNNING ──┬──▶ COMPLETED
                    ├──▶ FAILED     (fail_fast task failure, workflow timeout, exception)
                    └──▶ CANCELLED  (stop, shutdown, abort, or outer task cancellation)
```

Calling `shutdown()` or `abort()` **after** a workflow has already reached `COMPLETED` or `FAILED` preserves that terminal status.

## Error Handling, Branch Skipping & `fail_fast`

### `fail_fast=False` (Default)

- Task exceptions, non-zero shell exit codes, and per-step timeouts are captured into each step's `TaskRun` or `ShellProcess` (`status == RunStatus.FAILED`, `error` populated, `trace` populated for Python exceptions).
- **Explicit downstream dependencies are skipped safely**: if step `B` explicitly declared `dependencies=["A"]` (or `dependencies=[step_a]`) and `A` failed, `B`—and anything transitively depending on `B`—is not executed and is recorded with `status == RunStatus.CANCELLED` and an error explaining which upstream dependency failed.
- **Independent branches and later groups without explicit dependencies continue to run**: unrelated steps still complete normally.
- `orch.error` remains `None` and `orch.status` becomes `WorkflowStatus.COMPLETED`.

### `fail_fast=True`

- Works identically in both `wait=True` and `wait=False` modes.
- Triggers when any step finishes with `status == RunStatus.FAILED` (a shell command that exits `0` while writing warnings to `stderr` has `RunStatus.COMPLETE` and does **not** falsely trigger `fail_fast`).
- Immediately cancels and reaps any still-running sibling steps in the same wave.
- Records all completed results from the failing wave into `orch.results` and emits that batch to `async for` consumers.
- Skips all subsequent waves.
- Sets `orch.status = WorkflowStatus.FAILED`, `orch.error = OrchestralTaskFailure(...)`, and `orch.trace` to the failing task's traceback (when available).

```python
from orchestral import Orchestral, OrchestralTaskFailure, WorkflowStatus

async with Orchestral() as orch:
    with orch as workflow:
        s1 = workflow.add_command("false", alias="failing_cmd")
        workflow.add_command("echo", "never runs", alias="skipped", dependencies=[s1])

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

- **Step timeout**: Cancels only that step (terminating and reaping its subprocess if shell), marks that step's result as `RunStatus.FAILED` with a timeout error message, and obeys `fail_fast` / downstream skipping.
- **Workflow timeout**: Cancels all currently running steps/subprocesses in the active wave, sets `orch.status = WorkflowStatus.FAILED`, and sets `orch.error = TimeoutError(...)`.

## Streaming Wave Results (`async for`)

Iterate over `orch` with `async for` to receive each wave's results (`list[ShellProcess | TaskRun]`) as soon as that wave completes:

```python
async with Orchestral() as orch:
    with orch as workflow:
        workflow.add_command("echo", "first", alias="first")
        workflow.add_command("echo", "second", alias="second", dependencies=["first"])

    await orch.run(wait=False)
    async for batch in orch:
        for res in batch:
            print(res.task_name, "->", res.result.strip())
```

Guarantees:

- **Ordered & lossless**: Every completed wave's batch is preserved in order; fast consecutive waves never overwrite earlier batches.
- **Multiple consumers**: Multiple concurrent `async for` loops on the same `Orchestral` instance each see the complete sequence of waves.
- **Replay after completion**: Iterating after `await orch.run(wait=True)` yields all recorded wave batches and finishes immediately.
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
