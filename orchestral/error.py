class OrchestralError(Exception):
    """Base exception for orchestral errors."""


class OrchestralTaskFailure(OrchestralError):
    """Raised when a task fails while fail_fast is enabled."""

    def __init__(
        self,
        error: str,
        trace: str | None = None,
        *,
        task_name: str | None = None,
        run_id: int | None = None,
    ) -> None:
        self.error = error
        self.trace = trace
        self.task_name = task_name
        self.run_id = run_id
        super().__init__(error)


class OrchestralGraphError(OrchestralError, ValueError):
    """Raised when a workflow dependency graph is invalid."""


class OrchestralCycleError(OrchestralGraphError):
    """Raised when a dependency cycle is detected in a workflow graph."""

    def __init__(self, cycle: list[str]) -> None:
        self.cycle = list(cycle)
        cycle_path = " -> ".join(self.cycle)
        super().__init__(f"Dependency cycle detected: {cycle_path}")
