# Getting Started

## Requirements & Installation

`orchestral` requires **Python 3.11+** and depends on [`taskex`](https://pypi.org/project/taskex/) for result models (`ShellProcess`, `TaskRun`, `RunStatus`) and configuration (`Env`).

```bash
pip install orchestral
```

## Building a Workflow

An `Orchestral` instance manages a single active `Workflow` made up of one or more `Group`s.

- **Groups run sequentially** in the order they were created.
- **Steps inside a group run concurrently**.

You can build a workflow using either the `with orch as workflow:` builder context manager or `orch.create()`:

```python
import asyncio
from orchestral import Orchestral


async def main() -> None:
    async with Orchestral() as orch:
        with orch as workflow:
            g1 = workflow.group()
            g1.add_command("echo", "step 1a", alias="step_1a")
            g1.add_command("echo", "step 1b", alias="step_1b")

            g2 = workflow.group()
            g2.add_command("echo", "step 2", alias="step_2")

        await orch.run(wait=True)
        print(orch.status)


asyncio.run(main())
```

Using `orch.create()` is equivalent to entering `with orch as workflow:`:

```python
async with Orchestral() as orch:
    workflow = orch.create()
    group = workflow.group()
    group.add_command("echo", "hello")
    await orch.run(wait=True)
```

> **Tip:** You can instantiate `Orchestral()` and build a workflow outside an active asyncio event loop (for example, during application setup), then execute `await orch.run(...)` inside `asyncio.run(...)`.

## Adding Steps

### Shell Commands (`add_command`)

```python
group.add_command(
    "ls",
    "-la",
    alias="list_dir",
    cwd="/tmp",
    env={"LC_ALL": "C"},
    shell=False,
    timeout="5s",
)
```

- By default `shell=False`, executing the binary directly via `asyncio.create_subprocess_exec` without invoking a system shell (safe against shell injection).
- Set `shell=True` when you need shell features like pipes, redirection, or environment variable expansion (`group.add_command("echo $HOME", shell=True)`). Any extra positional arguments passed alongside `shell=True` are quoted with `shlex.quote`.

### Python Callables (`add_task`)

`add_task` accepts both `async def` coroutines (including async callable objects and `functools.partial` wrappers) and regular synchronous functions:

```python
async def fetch_item(item_id: int, *, verbose: bool = False) -> dict:
    await asyncio.sleep(0.05)
    return {"id": item_id, "verbose": verbose}


def double(x: int) -> int:
    return x * 2


group.add_task(fetch_item, 1, verbose=True, alias="fetch_1")
group.add_task(fetch_item, 2, verbose=False, alias="fetch_2")
group.add_task(double, 21, alias="double_21")
```

If your target function has a keyword parameter whose name overlaps with an `add_task` option (such as `timeout`, `alias`, or `schedule`), pre-bind it with `functools.partial`:

```python
import functools

group.add_task(functools.partial(http_get, timeout=3), "https://example.com", timeout="10s")
```

## Reading Results

After running a workflow, results are stored in `orch.results` as a nested dictionary:

```python
orch.results: dict[str, dict[int, ShellProcess | TaskRun]]
```

- Outer key: `task_name` (`alias` if given, otherwise the command string or callable's `__name__`).
- Inner key: unique integer snowflake `run_id`.
- Value: a `ShellProcess` (for shell steps) or `TaskRun` (for Python callables).

```python
from taskex.models import RunStatus

for task_name, runs in orch.results.items():
    for run_id, res in runs.items():
        if res.status == RunStatus.COMPLETE:
            print(f"{task_name} ({run_id}) -> {res.result!r} in {res.elapsed:.3f}s")
        else:
            print(f"{task_name} ({run_id}) FAILED: {res.error}")
```

## Blocking vs. Non-Blocking Execution

- `await orch.run(wait=True)`: awaits completion of the entire workflow before returning.
- `await orch.run(wait=False)` (default): starts the workflow as a background `asyncio.Task` and returns immediately. Use `async for batch in orch:` to stream completed groups or `await orch.wait()` to wait for completion.
