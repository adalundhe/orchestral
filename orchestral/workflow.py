from typing import Any, Iterator, Literal, TypeVar

from taskex import Env
from taskex.snowflake import SnowflakeGenerator

from .graph import ExecutionPlan, build_execution_plan
from .group import Group
from .runner import WorkflowRunner
from .step import DependenciesInput, Step, WorkflowTask
from .workflow_status import WorkflowStatus

T = TypeVar("T")


class Workflow:
    def __init__(
        self,
        config: Env | None = None,
    ) -> None:
        self._config = config
        self._groups: list[Group] = []
        self._default_group: Group | None = None
        self._snowflake_generator = SnowflakeGenerator(0)
        self._runner: WorkflowRunner | None = None
        self.status = WorkflowStatus.READY

    @property
    def runner(self) -> WorkflowRunner:
        if self._runner is None:
            self._runner = WorkflowRunner(config=self._config)
        return self._runner

    @property
    def has_runner(self) -> bool:
        return self._runner is not None

    @property
    def groups(self) -> list[Group]:
        return list(self._groups)

    def __iter__(self) -> Iterator[Group]:
        yield from self._groups

    def __len__(self) -> int:
        return len(self._groups)

    def group(self) -> Group:
        self._default_group = None
        group = Group(self._snowflake_generator.generate())
        self._groups.append(group)
        return group

    def _ensure_default_group(self) -> Group:
        if self._default_group is None:
            group = Group(self._snowflake_generator.generate())
            self._groups.append(group)
            self._default_group = group
        return self._default_group

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
        """Add a Python task step directly to the workflow DAG."""
        return self._ensure_default_group().add_task(
            step,
            *args,
            alias=alias,
            dependencies=dependencies,
            timeout=timeout,
            schedule=schedule,
            trigger=trigger,
            repeat=repeat,
            keep=keep,
            max_age=max_age,
            keep_policy=keep_policy,
            **kwargs,
        )

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
        """Add a shell/subprocess step directly to the workflow DAG."""
        return self._ensure_default_group().add_command(
            step,
            *args,
            alias=alias,
            dependencies=dependencies,
            env=env,
            cwd=cwd,
            shell=shell,
            timeout=timeout,
            schedule=schedule,
            trigger=trigger,
            repeat=repeat,
            keep=keep,
            max_age=max_age,
            keep_policy=keep_policy,
        )

    def build_plan(self) -> ExecutionPlan:
        """Validate the dependency graph and return the full ``ExecutionPlan``."""
        return build_execution_plan([group.steps for group in self._groups])

    def build_waves(self) -> list[list[Step[Any]]]:
        """Validate the dependency graph and return ordered concurrent execution waves."""
        return self.build_plan().waves

    def execution_order(self) -> list[list[str]]:
        """Validate the dependency graph and return wave-by-wave step names."""
        return self.build_plan().execution_order()
