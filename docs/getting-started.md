# Getting Started

## Requirements & Installation

`orchestral` requires **Python 3.11+** and depends on [`taskex`](https://pypi.org/project/taskex/) for result models (`ShellProcess`, `TaskRun`, `RunStatus`) and configuration (`Env`).

```bash
pip install orchestral
```

## Building a Workflow

An `Orchestral` instance manages an active `Workflow`. You can express execution order in two complementary ways:

1. **Dependency-graph waves**: Add steps directly to `workflow` (or inside a `Group`) with `dependencies=[...]`. `orchestral` validates the DAG and groups steps into parallel execution waves via topological sort.
2. **Explicit sequential groups**: Create stages with `workflow.group()`. Groups run strictly in order, and steps within a group run concurrently (or in intra-group waves if they declare dependencies).

### DAG Workflow (Direct on `Workflow`)

```python
import asyncio
from orchestral import Orchestral


async def fetch_users() -> list[str]:
    await asyncio.sleep(0.05)
    return ["alice", "bob"]


async def fetch_orders() -> list[int]:
    await asyncio.sleep(0.05)
    return [101, 102]


async def build_report() -> str:
    return "report-ready"


async def main() -> None:
    async with Orchestral() as orch:
        with orch as workflow:
            u = workflow.add_task(fetch_users)
            o = workflow.add_task(fetch_orders)
            r = workflow.add_task(build_report, dependencies=[u, o])
            workflow.add_command("echo", "sent", alias="notify", dependencies=[r])

        print("Planned waves:", orch.execution_order())
        # [["fetch_users", "fetch_orders"], ["build_report"], ["notify"]]

        await orch.run(wait=True, fail_fast=True)
        print("Status:", orch.status)


asyncio.run(main())
```

### Grouped Workflow

```python
async with Orchestral() as orch:
    with orch as workflow:
        g1 = workflow.group()
        g1.add_command("echo", "step 1a", alias="step_1a")
        g1.add_command("echo", "step 1b", alias="step_1b")

        g2 = workflow.group()
        g2.add_command("echo", "step 2", alias="step_2")

    await orch.run(wait=True)
```

Using `orch.create()` is equivalent to entering `with orch as workflow:`:

```python
async with Orchestral() as orch:
    workflow = orch.create()
    workflow.add_command("echo", "hello")
    await orch.run(wait=True)
```

> **Tip:** You can instantiate `Orchestral()` and build a workflow outside an active asyncio event loop (for example, during application setup), then execute `await orch.run(...)` inside `asyncio.run(...)`.

## Declaring Dependencies

`dependencies` on `add_task` and `add_command` accepts any of the following (either as a single value or an iterable):

- **A `Step` object** returned by an earlier `add_task(...)` or `add_command(...)` (matches that exact step instance, even if multiple steps share a name).
- **A step name or `alias` string** (e.g., `dependencies=["build", "lint"]` or `dependencies="build"`).
- **A callable reference** (e.g., `dependencies=[fetch_users]`, matched by its resolved function name).

Steps within the same group/DAG can be declared in any order—downstream steps may even be registered before the upstream steps they depend on.

## Adding Steps

### Shell Commands (`add_command`)

```python
group.add_command(
    "ls",
    "-la",
    alias="list_dir",
    dependencies=["prepare"],
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
group.add_task(double, 21, alias="double_21", dependencies=["fetch_1", "fetch_2"])
```

If your target function has a keyword parameter whose name overlaps with an `add_task` option (such as `timeout`, `alias`, `dependencies`, or `schedule`), pre-bind it with `functools.partial`:

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
            print(f"{task_name} ({run_id}) {res.status.value}: {res.error}")
```

## Blocking vs. Non-Blocking Execution

- `await orch.run(wait=True)`: awaits completion of the entire workflow before returning.
- `await orch.run(wait=False)` (default): starts the workflow as a background `asyncio.Task` and returns immediately. Use `async for batch in orch:` to stream completed waves or `await orch.wait()` to wait for completion.
