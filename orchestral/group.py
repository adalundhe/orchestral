from typing import Any, Iterator, Literal, TypeVar

from .graph import build_execution_plan
from .step import DependenciesInput, Step, WorkflowTask

T = TypeVar("T")


class Group:
    def __init__(
        self,
        group_id: int,
    ) -> None:
        self.id = group_id
        self._steps: list[Step[Any]] = []

    def __iter__(self) -> Iterator[Step[Any]]:
        yield from self._steps

    def __len__(self) -> int:
        return len(self._steps)

    @property
    def steps(self) -> list[Step[Any]]:
        return list(self._steps)

    def build_waves(self) -> list[list[Step[Any]]]:
        """Validate intra-group dependencies and return parallel execution waves."""
        return build_execution_plan([self._steps]).waves

    def execution_order(self) -> list[list[str]]:
        """Return the wave-by-wave step names for this group."""
        return build_execution_plan([self._steps]).execution_order()

    def add_task(
        self,
        step: WorkflowTask[T],
        *args: Any,
        alias: str | None = None,
        dependencies: DependenciesInput = None,
        timeout: str | int | float | None = None,
        schedule: str | int | float | None = None,
        trigger: Literal["MANUAL", "ON_START"] = "MANUAL",
        repeat: int | Literal["NEVER", "ALWAYS"] = "NEVER",
        keep: int | None = None,
        max_age: str | int | float | None = None,
        keep_policy: Literal["COUNT", "AGE", "COUNT_AND_AGE"] = "COUNT",
        **kwargs: Any,
    ) -> Step[T]:
        created = Step(
            step,
            dependencies=dependencies,
            args=args,
            kwargs=kwargs,
            alias=alias,
            timeout=timeout,
            schedule=schedule,
            trigger=trigger,
            repeat=repeat,
            keep=keep,
            max_age=max_age,
            keep_policy=keep_policy,
        )
        self._steps.append(created)
        return created

    def add_command(
        self,
        step: str,
        *args: str,
        alias: str | None = None,
        dependencies: DependenciesInput = None,
        env: dict[str, Any] | None = None,
        cwd: str | None = None,
        shell: bool = False,
        timeout: str | int | float | None = None,
        schedule: str | int | float | None = None,
        trigger: Literal["MANUAL", "ON_START"] = "MANUAL",
        repeat: int | Literal["NEVER", "ALWAYS"] = "NEVER",
        keep: int | None = None,
        max_age: str | int | float | None = None,
        keep_policy: Literal["COUNT", "AGE", "COUNT_AND_AGE"] = "COUNT",
    ) -> Step[str]:
        if not isinstance(step, str):
            raise TypeError(
                f"Command must be a string, got {type(step).__name__}"
            )

        created: Step[str] = Step(
            step,
            dependencies=dependencies,
            shell_args=args,
            env=env,
            cwd=cwd,
            shell=shell,
            alias=alias,
            timeout=timeout,
            schedule=schedule,
            trigger=trigger,
            repeat=repeat,
            keep=keep,
            max_age=max_age,
            keep_policy=keep_policy,
        )
        self._steps.append(created)
        return created
