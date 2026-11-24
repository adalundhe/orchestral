# Checkpointing, Pause & Resume

`orchestral` builds directly on `taskex >= 0.5.0` to provide **live suspension/resumption**, **in-task and external checkpointing**, and **durable workflow recovery** across async coroutines, thread-pool sync functions, shell subprocesses, and repeating schedules.

---

## Live Pause & Resume

### Pausing and Resuming an Entire Workflow

While a workflow is running (typically started with `wait=False`), call `await orch.pause()` and `await orch.resume()`:

```python
await orch.run(wait=False, timeout="5s")

# Suspend active steps and prevent subsequent waves from starting
await orch.pause()
assert orch.status == WorkflowStatus.PAUSED
assert orch.is_paused is True

# ... do maintenance or wait for external signal ...

# Resume paused steps and continue remaining waves (optionally waiting to completion)
await orch.resume(wait=True)
```

How each workload suspends under `await orch.pause()` / `await orch.pause_step(...)`:

| Step Type | Pause Behavior | Resume Behavior |
| --- | --- | --- |
| **Async task** | Marks `RunStatus.PAUSED`; suspends at `await pause_point()` or `await checkpoint()` | Unblocks suspended coroutine in-place |
| **Sync task (`"thread"` / `"none"`)** | Marks `RunStatus.PAUSED`; blocks worker thread at `pause_point_sync()` or `checkpoint_sync()` | Unblocks worker thread in-place |
| **Shell command (POSIX)** | Sends `SIGSTOP` to the subprocess process group (or defers spawn) | Sends `SIGCONT` to the process group |
| **Repeating schedule** | Suspends the schedule loop and pauses the current iteration's run | Resumes the current run and schedule loop |

### Pausing a Single Step

Use `pause_step()` and `resume_step()` to suspend one step (by `Step` object, step name/alias, or run token) while allowing sibling steps in the same wave to keep running:

```python
await orch.pause_step("slow_step")
# Sibling steps in the wave continue running...
await orch.resume_step("slow_step")
```

### Pause-Aware Timeouts & Cancellation Safety

- Both **step-level** and **workflow-level** timeouts freeze while paused, so pausing longer than the configured timeout will not trigger a false timeout.
- `orch.paused_duration` and each result's `res.paused_duration` record the cumulative time spent paused.
- Calling `await orch.stop()`, `await orch.shutdown()`, or `orch.abort()` while paused safely unblocks waiting sync threads, resumes + terminates `SIGSTOP`-paused POSIX subprocesses, and marks the workflow `WorkflowStatus.CANCELLED`.

---

## In-Task Checkpointing Helpers

All `taskex` `ContextVar` helpers are re-exported directly from `orchestral` so tasks can checkpoint and cooperate with pause without changing their function signature:

### Async Tasks

```python
from orchestral import checkpoint, last_checkpoint, pause_point


async def process_items(items: list[str]) -> list[str]:
    prev = last_checkpoint()
    start_idx = prev.state["index"] if prev else 0
    processed = list(prev.state["processed"]) if prev else []

    for i in range(start_idx, len(items)):
        await pause_point()
        processed.append(items[i].upper())
        await checkpoint(
            {"index": i + 1, "processed": processed},
            metadata={"progress": (i + 1) / len(items)},
        )

    return processed
```

`checkpoint(state, metadata=None, check_pause=True)` persists the checkpoint to the configured `CheckpointStore`, updates the run's latest checkpoint, and honors any pending pause if `check_pause=True`.

### Sync Tasks (`"thread"` or `"none"` Executor)

```python
from orchestral import checkpoint_sync, last_checkpoint, pause_point_sync


def process_batch(items: list[str]) -> list[str]:
    prev = last_checkpoint()
    start_idx = prev.state["index"] if prev else 0
    processed = list(prev.state["processed"]) if prev else []

    for i in range(start_idx, len(items)):
        pause_point_sync()
        processed.append(items[i].upper())
        checkpoint_sync({"index": i + 1, "processed": processed})

    return processed
```

> **Note:** `contextvars.Context` cannot cross OS process boundaries. In-task `checkpoint_sync()` / `pause_point_sync()` / `last_checkpoint()` require `MERCURY_SYNC_EXECUTOR_TYPE="thread"` or `"none"`. For `"process"` tasks, use external checkpointing via `await orch.checkpoint(target, state)`.

---

## Checkpoint Stores

Configure persistence on `Orchestral`:

1. **`MemoryCheckpointStore` (default)**: Automatic when neither `checkpoint_store` nor `MERCURY_SYNC_CHECKPOINT_DB_PATH` is set. Owned and closed by `Orchestral`.
2. **`SQLiteCheckpointStore` via `Env`**: Pass `Env(MERCURY_SYNC_CHECKPOINT_DB_PATH="./state/checkpoints.db")`. Owned and closed by `Orchestral` on `shutdown()`/`abort()`.
3. **Explicit `checkpoint_store`**: Pass a `MemoryCheckpointStore`, `SQLiteCheckpointStore`, or custom sync/async `CheckpointStore` to `Orchestral(checkpoint_store=store, workflow_id="...")`. The caller owns closing `store`.

```python
from orchestral import Env, Orchestral, SQLiteCheckpointStore

# Option A: Orchestral-managed SQLite database
cfg = Env(
    MERCURY_SYNC_EXECUTOR_TYPE="none",
    MERCURY_SYNC_CHECKPOINT_DB_PATH="./state/checkpoints.db",
)
async with Orchestral(config=cfg, workflow_id="billing-etl") as orch:
    ...

# Option B: Caller-managed SQLite store (inspectable after shutdown)
with SQLiteCheckpointStore("./state/checkpoints.db", max_checkpoints_per_run=50) as store:
    async with Orchestral(checkpoint_store=store, workflow_id="billing-etl") as orch:
        ...
```

---

## Recovering Failed or Cancelled Workflows

When `await orch.resume(wait=True, ...)` is called on a workflow that is **not** currently live-paused (for example, after a failure, cancellation, or in a brand-new `Orchestral` instance / process sharing the same `SQLiteCheckpointStore` and `workflow_id`):

1. `orchestral` loads the latest persisted workflow checkpoint for `workflow_id`.
2. Already-completed steps from earlier waves are **not re-executed** (unless `rerun_completed=True`); their results are restored into `orch.results`.
3. Every remaining incomplete/failed step is launched seeded with its latest task `Checkpoint`, so `last_checkpoint()` inside the step returns that checkpoint immediately.
4. Remaining downstream waves run in dependency order once their upstream dependencies complete.

> **Note on non-JSON-serializable return values:** When using `SQLiteCheckpointStore`, step checkpoints saved via `checkpoint(state)` must have JSON-serializable `state` and `metadata`. For workflow-level step return values, if a step returns a non-JSON-serializable Python object, `orchestral` still records step completion safely (storing `result=None` with `result_serialized=False` in SQLite while retaining the original object in memory).

### Explicit Step-Level `resume_from`

You can also seed any individual step explicitly with a run token (`"task_name:run_id"`), task name (`"task_name"`), or `Checkpoint` instance—compatible with both sync and async `CheckpointStore` implementations:

```python
workflow.add_task(process_items, items, alias="etl", resume_from="etl")
# or:
workflow.add_task(process_items, items, alias="etl", resume_from=previous_token)
```

---

## External Checkpoint Management

Inspect, write, or clear checkpoints from outside a task:

```python
cp = await orch.checkpoint("etl", {"stage": "uploaded"}, metadata={"attempt": 1})
latest = await orch.get_checkpoint("etl")          # by step name, Step, or token
history = await orch.list_checkpoints(token)       # all checkpoints for a run token
await orch.clear_checkpoints(token)                # clear one run's checkpoints
await orch.clear_checkpoints()                     # clear entire workflow's checkpoints
```
