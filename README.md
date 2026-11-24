# orchestral

Orchestrate and execute shell commands, async coroutines, and sync Python functions in structured, concurrent workflows.

[![PyPI version](https://img.shields.io/pypi/v/orchestral?color=blue)](https://pypi.org/project/orchestral/)
[![License](https://img.shields.io/github/license/adalundhe/orchestral)](https://github.com/adalundhe/orchestral/blob/main/LICENSE)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/orchestral?color=red)](https://pypi.org/project/orchestral/)

| Package  | orchestral                                       |
| -------- | ------------------------------------------------ |
| Version  | 0.1.2                                            |
| Download | https://pypi.org/project/orchestral/             |
| Source   | https://github.com/adalundhe/orchestral          |
| Keywords | async, tasks, workflow, runner, graph, shell, jobs |

## Features

- **Unified task orchestration**: Mix shell commands (`add_command`), `async def` coroutines, and synchronous Python callables (`add_task`) in the same workflow.
- **Sequential groups, concurrent steps**: Groups execute strictly in order; all steps inside a group execute concurrently.
- **Streaming batch results**: Consume each group's results as it completes using `async for batch in orch:`.
- **Fail-fast & error capture**: Run to completion by default or enable `fail_fast=True` to cancel in-flight siblings immediately and stop subsequent groups on the first failure.
- **Timeouts at every level**: Enforce deadlines on individual steps or the entire workflow using numbers (seconds) or duration strings (`"500ms"`, `"5s"`, `"1m30s"`).
- **Safe process & resource lifecycle**: Automatically terminates and reaps subprocesses (including child process groups on POSIX), shuts down executors cleanly, and supports `async with`, `stop()`, `shutdown()`, and `abort()`.
- **Scheduled / repeating steps**: Repeat individual steps on an interval with bounded history retention (`COUNT`, `AGE`, or `COUNT_AND_AGE`).

## Installation

Requires Python 3.11 or newer.

```bash
pip install orchestral
```

Or with `uv`:

```bash
uv add orchestral
```

## Quick Start

```python
import asyncio
from taskex import Env
from orchestral import Orchestral, WorkflowStatus


async def fetch_greeting(name: str) -> str:
    await asyncio.sleep(0.05)
    return f"Hello, {name}!"


def compute_checksum(text: str) -> int:
    return sum(ord(ch) for ch in text)


async def main() -> None:
    config = Env(MERCURY_SYNC_EXECUTOR_TYPE="thread")

    async with Orchestral(config=config) as orch:
        with orch as workflow:
            # Group 1: runs concurrently
            prepare = workflow.group()
            prepare.add_command("echo", "starting pipeline", alias="banner")
            prepare.add_task(fetch_greeting, "orchestral", alias="greet")

            # Group 2: starts only after Group 1 finishes
            analyze = workflow.group()
            analyze.add_task(compute_checksum, "orchestral", alias="checksum")

        await orch.run(wait=True, fail_fast=True, timeout="30s")

        print("Status:", orch.status)
        print("Elapsed:", f"{orch.elapsed:.3f}s")
        for task_name, runs in orch.results.items():
            for run_id, result in runs.items():
                print(task_name, run_id, result.status, result.result)


asyncio.run(main())
```

## Common Patterns

### Non-blocking runs & streaming batch results

```python
async with Orchestral() as orch:
    with orch as workflow:
        g1 = workflow.group()
        g1.add_command("ls", alias="list_files")
        g1.add_command("pwd", alias="working_dir")

        g2 = workflow.group()
        g2.add_command("echo", "done", alias="finish")

    await orch.run(wait=False)

    async for batch in orch:
        print("Completed group:", [(r.task_name, r.status.value) for r in batch])
```

### Explicit lifecycle management

If you prefer not to use `async with`, always call `await orch.shutdown()` (or synchronous `orch.abort()` in emergency teardown) when finished:

```python
orch = Orchestral()
try:
    with orch as workflow:
        group = workflow.group()
        group.add_command("echo", "hello")
    await orch.run(wait=True)
finally:
    await orch.shutdown()
```

### Error handling & `fail_fast`

- `fail_fast=False` (default): individual failures are recorded on their `TaskRun` / `ShellProcess` entries in `orch.results` with `status == RunStatus.FAILED`, while remaining steps and groups continue to run.
- `fail_fast=True`: the first failed step immediately cancels remaining in-flight siblings in the current group, skips subsequent groups, records the completed results from the failing group, sets `orch.status = WorkflowStatus.FAILED`, and populates `orch.error` with an `OrchestralTaskFailure` (and `orch.trace` with the traceback).

### Executor configuration

Synchronous functions passed to `add_task()` run in either a `ProcessPoolExecutor` (default) or a `ThreadPoolExecutor`, configured via `taskex.Env`:

```python
from taskex import Env
from orchestral import Orchestral

# Use threads for closures, lambdas, bound methods, or I/O-bound sync code
orch = Orchestral(
    config=Env(
        MERCURY_SYNC_EXECUTOR_TYPE="thread",
        MERCURY_SYNC_TASK_RUNNER_MAX_THREADS=4,
    )
)
```

> **Note:** When using the default `"process"` executor, sync callables and their arguments must be picklable (e.g. top-level module functions). Pure shell and async workflows never spawn an executor.

## Documentation

Detailed guides and API reference live in [`docs/`](docs/index.md):

- [Documentation Index](docs/index.md)
- [Getting Started](docs/getting-started.md)
- [Execution Model & Configuration](docs/execution-model.md)
- [Error Handling, Timeouts & Lifecycle](docs/error-handling-and-lifecycle.md)
- [API Reference](docs/api-reference.md)

Runnable examples are available in [`examples/`](examples/):

- [`examples/shell_workflow.py`](examples/shell_workflow.py)
- [`examples/mixed_workflow.py`](examples/mixed_workflow.py)

## Development & Testing

```bash
uv venv --python 3.13
uv pip install -r requirements.txt pytest pytest-asyncio
PYTHONPATH=. .venv/bin/pytest -v tests/
```

## License

MIT — see [LICENSE](LICENSE).
