# orchestral Documentation

Welcome to the `orchestral` documentation. `orchestral` is an asynchronous Python workflow orchestration library for running shell commands, `async` coroutines, and synchronous functions in ordered concurrent groups.

## Guides

1. **[Getting Started](getting-started.md)** — Installation, building workflows with `with orch as workflow:` or `orch.create()`, running workflows, and reading results.
2. **[Execution Model & Configuration](execution-model.md)** — Groups vs. steps, shell vs. async vs. sync execution, `taskex.Env` executor tuning, and scheduled/repeating steps with retention policies.
3. **[Error Handling, Timeouts & Lifecycle](error-handling-and-lifecycle.md)** — `WorkflowStatus` state machine, `fail_fast`, duration strings, streaming batches via `async for`, and leak-free `stop()` / `shutdown()` / `abort()`.
4. **[API Reference](api-reference.md)** — Complete reference for `Orchestral`, `Workflow`, `Group`, `Step`, `StepType`, `WorkflowStatus`, `OrchestralTaskFailure`, `WorkflowRunner`, and `taskex` result types.

## Examples

- [`examples/shell_workflow.py`](../examples/shell_workflow.py) — Basic shell command pipeline across two groups.
- [`examples/mixed_workflow.py`](../examples/mixed_workflow.py) — Shell, async, and sync steps with streaming results and automatic teardown.
