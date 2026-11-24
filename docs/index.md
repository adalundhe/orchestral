# orchestral Documentation

Welcome to the `orchestral` documentation. `orchestral` is an asynchronous Python workflow orchestration library for running shell commands, `async` coroutines, and synchronous functions in dependency-graph waves and ordered groups.

## Guides

1. **[Getting Started](getting-started.md)** — Installation, building DAG or grouped workflows, declaring step dependencies, running workflows, and reading results.
2. **[Execution Model & Configuration](execution-model.md)** — Dependency-graph wave resolution, group barriers, shell vs. async vs. sync execution, `taskex.Env` executor tuning, and scheduled/repeating steps with retention policies.
3. **[Error Handling, Timeouts & Lifecycle](error-handling-and-lifecycle.md)** — Graph validation errors, `WorkflowStatus` state machine, `fail_fast` vs. branch skipping, duration strings, streaming waves via `async for`, and leak-free `stop()` / `shutdown()` / `abort()`.
4. **[API Reference](api-reference.md)** — Complete reference for `Orchestral`, `Workflow`, `Group`, `Step`, `ExecutionPlan`, `StepType`, `WorkflowStatus`, `OrchestralError`/`OrchestralGraphError`/`OrchestralCycleError`/`OrchestralTaskFailure`, `WorkflowRunner`, and `taskex` result types.

## Examples

- [`examples/dag_workflow.py`](../examples/dag_workflow.py) — Diamond dependency graph executed in parallel waves with `execution_order()` and live wave streaming.
- [`examples/shell_workflow.py`](../examples/shell_workflow.py) — Basic shell command pipeline across two sequential groups.
- [`examples/mixed_workflow.py`](../examples/mixed_workflow.py) — Shell, async, and sync steps with streaming results and automatic teardown.
