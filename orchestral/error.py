class OrchestralTaskFailure(Exception):
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
