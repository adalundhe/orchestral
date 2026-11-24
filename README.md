# orchestral

Orchestrate and execute shell commands, async coroutines, and sync Python functions in dependency-graph waves and sequential groups.

[![PyPI version](https://img.shields.io/pypi/v/orchestral?color=blue)](https://pypi.org/project/orchestral/)
[![License](https://img.shields.io/github/license/adalundhe/orchestral)](https://github.com/adalundhe/orchestral/blob/main/LICENSE)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/orchestral?color=red)](https://pypi.org/project/orchestral/)

| Package  | orchestral                                       |
| -------- | ------------------------------------------------ |
| Version  | 0.3.0                                            |
| Download | https://pypi.org/project/orchestral/             |
| Source   | https://github.com/adalundhe/orchestral          |
| Keywords | async, tasks, workflow, runner, graph, shell, jobs |

## Features

- **Unified task orchestration**: Mix shell commands (`add_command`), `async def` coroutines, and synchronous Python callables (`add_task`) in the same workflow.
- **Dependency-graph wave execution**: Declare step `dependencies` by name/alias, callable, or `Step` reference; `orchestral` validates the DAG (detecting missing dependencies and cycles upfront) and executes topologically sorted parallel waves.
- **Explicit sequential groups**: Organize stages with `workflow.group()` when you want strict stage barriers, with optional intra-group or cross-group dependencies.
- **Streaming wave results**: Consume each wave's results as it completes using `async for batch in orch:`.
- **Fail-fast & branch-aware failure handling**: Under `fail_fast=True`, failures immediately cancel in-flight siblings and halt remaining waves; under `fail_fast=False`, independent branches continue while steps with failed explicit upstream dependencies are safely skipped (`RunStatus.CANCELLED`).
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

### Dependency-graph (DAG) workflow

```python
import asyncio
from orchestral import Orchestral


async def build() -> str:
    await asyncio.sleep(0.05)
    return "artifact.whl"


async def unit_tests() -> str:
    await asyncio.sleep(0.05)
    return "unit:ok"


async def integration_tests() -> str:
    await asyncio.sleep(0.05)
    return "integration:ok"


async def main() -> None:
    async with Orchestral() as orch:
        with orch as workflow:
            s_build = workflow.add_task(build)
            s_unit = workflow.add_task(unit_tests, dependencies=[s_build])
            s_int = workflow.add_task(integration_tests, dependencies=["build"])
            workflow.add_command(
                "echo",
                "deploying",
                alias="deploy",
                dependencies=[s_unit, s_int],
            )

        # Inspect planned parallel waves before running:
        # [["build"], ["unit_tests", "integration_tests"], ["deploy"]]
        print("Waves:", orch.execution_order())

        await orch.run(wait=True, fail_fast=True, timeout="30s")
        print("Status:", orch.status, f"({orch.elapsed:.3f}s)")


asyncio.run(main())
```

### Explicit sequential groups

```python
import asyncio
from taskex import Env
from orchestral import Orchestral


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
        for task_name, runs in orch.results.items():
            for run_id, result in runs.items():
                print(task_name, run_id, result.status, result.result)


asyncio.run(main())
```

## Common Patterns

### Non-blocking runs & streaming wave results

```python
async with Orchestral() as orch:
    with orch as workflow:
        workflow.add_command("ls", alias="list_files")
        workflow.add_command("pwd", alias="working_dir")
        workflow.add_command(
            "echo",
            "done",
            alias="finish",
            dependencies=["list_files", "working_dir"],
        )

    await orch.run(wait=False)

    async for batch in orch:
        print("Completed wave:", [(r.task_name, r.status.value) for r in batch])
```

### Explicit lifecycle management

If you prefer not to use `async with`, always call `await orch.shutdown()` (or synchronous `orch.abort()` in emergency teardown) when finished:

```python
orch = Orchestral()
try:
    with orch as workflow:
        workflow.add_command("echo", "hello")
    await orch.run(wait=True)
finally:
    await orch.shutdown()
```

### Error handling & `fail_fast`

- **Graph validation errors**: Missing dependencies, forward references to later groups, and dependency cycles raise `OrchestralGraphError` / `OrchestralCycleError` before any step runs.
- **`fail_fast=False` (default)**: Individual failures are recorded on their `TaskRun` / `ShellProcess` entries in `orch.results` with `status == RunStatus.FAILED`. Steps that explicitly depend on a failed upstream step are skipped (`RunStatus.CANCELLED`), while independent branches continue to run.
- **`fail_fast=True`**: The first failed step immediately cancels remaining in-flight siblings in the current wave, skips subsequent waves, records the completed results from the failing wave, sets `orch.status = WorkflowStatus.FAILED`, and populates `orch.error` with an `OrchestralTaskFailure` (and `orch.trace` with the traceback).

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

- [`examples/dag_workflow.py`](examples/dag_workflow.py)
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
