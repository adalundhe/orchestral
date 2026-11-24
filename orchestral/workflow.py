from typing import Iterator

from taskex import Env
from taskex.snowflake import SnowflakeGenerator

from .group import Group
from .runner import WorkflowRunner
from .workflow_status import WorkflowStatus


class Workflow:
    def __init__(
        self,
        config: Env | None = None,
    ) -> None:
        self._config = config
        self._groups: list[Group] = []
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
        group = Group(self._snowflake_generator.generate())
        self._groups.append(group)
        return group
