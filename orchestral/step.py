import inspect
from typing import (
    Any,
    Awaitable,
    Callable,
    Generic,
    Literal,
    TypeVar,
)

from .step_type import StepType
from .util import is_async_callable, parse_duration, resolve_callable_name

T = TypeVar("T")

WorkflowTask = Callable[..., Awaitable[T]] | Callable[..., T]
WorkflowStep = str | WorkflowTask[T]

_VALID_TRIGGERS = ("MANUAL", "ON_START")
_VALID_KEEP_POLICIES = ("COUNT", "AGE", "COUNT_AND_AGE")


class Step(Generic[T]):
    def __init__(
        self,
        step: WorkflowStep[T],
        *dependencies: WorkflowStep[Any],
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        shell_args: tuple[str, ...] = (),
        env: dict[str, Any] | None = None,
        cwd: str | None = None,
        shell: bool = False,
        alias: str | None = None,
        timeout: str | int | float | None = None,
        schedule: str | int | float | None = None,
        trigger: Literal["MANUAL", "ON_START"] = "MANUAL",
        repeat: int | Literal["NEVER", "ALWAYS"] = "NEVER",
        keep: int | None = None,
        max_age: str | int | float | None = None,
        keep_policy: Literal["COUNT", "AGE", "COUNT_AND_AGE"] = "COUNT",
    ) -> None:
        if isinstance(step, str):
            if not step.strip():
                raise ValueError("Command string cannot be empty")
            self.type = StepType.SHELL
        elif inspect.iscoroutine(step):
            raise TypeError(
                "Step must be a callable, not a coroutine object; "
                "pass the async function rather than calling it"
            )
        elif callable(step):
            self.type = StepType.ASYNC if is_async_callable(step) else StepType.SYNC
        else:
            raise TypeError(
                f"Step must be a command string or callable, got {type(step).__name__}"
            )

        if trigger not in _VALID_TRIGGERS:
            raise ValueError(
                f"trigger must be one of {_VALID_TRIGGERS}, got {trigger!r}"
            )

        if keep_policy not in _VALID_KEEP_POLICIES:
            raise ValueError(
                f"keep_policy must be one of {_VALID_KEEP_POLICIES}, got {keep_policy!r}"
            )

        if isinstance(repeat, bool) or (
            not isinstance(repeat, int) and repeat not in ("NEVER", "ALWAYS")
        ):
            raise ValueError(
                f"repeat must be 'NEVER', 'ALWAYS', or a positive integer, got {repeat!r}"
            )
        if isinstance(repeat, int) and repeat <= 0:
            raise ValueError(f"repeat integer must be positive, got {repeat!r}")

        if keep is not None:
            if isinstance(keep, bool) or not isinstance(keep, int) or keep <= 0:
                raise ValueError(f"keep must be a positive integer or None, got {keep!r}")

        self.timeout_seconds = parse_duration(timeout, name="timeout")
        self.schedule_seconds = parse_duration(schedule, name="schedule")
        self.max_age_seconds = parse_duration(max_age, name="max_age")

        if repeat == "ALWAYS" and self.schedule_seconds is None:
            raise ValueError("schedule must be provided when repeat is 'ALWAYS'")

        if alias is not None and (not isinstance(alias, str) or not alias):
            raise ValueError("alias must be a non-empty string or None")

        self.call = step
        self.args = tuple(args)
        self.kwargs: dict[str, Any] = dict(kwargs) if kwargs is not None else {}
        self.dependencies = [resolve_callable_name(dep) for dep in dependencies]

        self.alias = alias
        self.name = alias if alias is not None else resolve_callable_name(step)
        self.timeout = timeout
        self.schedule = schedule
        self.trigger = trigger
        self.repeat = repeat
        self.keep = keep
        self.max_age = max_age
        self.keep_policy = keep_policy

        self.env = dict(env) if env is not None else None
        self.cwd = cwd
        self.shell = shell
        self.shell_args = tuple(str(arg) for arg in shell_args)
