import functools
import pytest

from orchestral import Group, Step, StepType, Workflow
from orchestral.util import is_async_callable, parse_duration, resolve_callable_name


def sync_fn(a: int, b: int = 1) -> int:
    return a + b


async def async_fn(x: int) -> int:
    return x * 2


class AsyncCallable:
    async def __call__(self, x: int) -> int:
        return x + 10


class SyncCallable:
    def __call__(self, x: int) -> int:
        return x + 20


class TestDurationParser:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, None),
            (0, 0.0),
            (0.0, 0.0),
            (5, 5.0),
            (1.25, 1.25),
            ("0s", 0.0),
            ("500ms", 0.5),
            ("2s", 2.0),
            ("1.5s", 1.5),
            ("1m30s", 90.0),
            ("1h 2m 3s", 3723.0),
            ("1d", 86400.0),
            ("1w", 604800.0),
            ("1s 2s", 3.0),
            ("10", 10.0),
        ],
    )
    def test_valid_durations(self, raw, expected):
        assert parse_duration(raw) == expected

    @pytest.mark.parametrize(
        "bad",
        ["", "   ", "abc", "-5s", "5x", "10sec", -1, -0.1, float("nan"), float("inf")],
    )
    def test_invalid_duration_values(self, bad):
        with pytest.raises(ValueError):
            parse_duration(bad)

    @pytest.mark.parametrize("bad_type", [True, False, [], {}])
    def test_invalid_duration_types(self, bad_type):
        with pytest.raises(TypeError):
            parse_duration(bad_type)


class TestStepAndGroup:
    def test_step_type_detection(self):
        assert Step("echo").type == StepType.SHELL
        assert Step(sync_fn).type == StepType.SYNC
        assert Step(async_fn).type == StepType.ASYNC
        assert Step(functools.partial(async_fn, 1)).type == StepType.ASYNC
        assert Step(functools.partial(sync_fn, 1)).type == StepType.SYNC
        assert Step(AsyncCallable()).type == StepType.ASYNC
        assert Step(SyncCallable()).type == StepType.SYNC
        assert is_async_callable(AsyncCallable()) is True
        assert is_async_callable(SyncCallable()) is False

    def test_step_name_resolution(self):
        assert Step("echo").name == "echo"
        assert Step("echo", alias="greet").name == "greet"
        assert Step(sync_fn).name == "sync_fn"
        assert Step(functools.partial(sync_fn, 1)).name == "sync_fn"
        assert Step(AsyncCallable()).name == "AsyncCallable"
        assert resolve_callable_name(functools.partial(functools.partial(async_fn))) == "async_fn"

    def test_step_rejects_invalid_inputs(self):
        with pytest.raises(ValueError):
            Step("")
        with pytest.raises(ValueError):
            Step("   ")
        with pytest.raises(TypeError):
            Step(123)  # type: ignore[arg-type]

        coro = async_fn(1)
        try:
            with pytest.raises(TypeError, match="coroutine object"):
                Step(coro)  # type: ignore[arg-type]
        finally:
            coro.close()

    def test_step_validates_options(self):
        with pytest.raises(ValueError):
            Step("echo", timeout=-1)
        with pytest.raises(ValueError):
            Step("echo", timeout="bogus")
        with pytest.raises(ValueError):
            Step("echo", trigger="INVALID")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            Step("echo", keep_policy="INVALID")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            Step("echo", repeat=0)
        with pytest.raises(ValueError):
            Step("echo", repeat=-2)
        with pytest.raises(ValueError):
            Step("echo", repeat=True)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            Step("echo", repeat="ALWAYS")  # missing schedule
        with pytest.raises(ValueError):
            Step("echo", keep=0)
        with pytest.raises(ValueError):
            Step("echo", keep=True)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            Step("echo", alias="")

    def test_step_dependencies_with_partial(self):
        step = Step(sync_fn, "setup", async_fn, functools.partial(sync_fn, 1))
        assert step.dependencies == ["setup", "async_fn", "sync_fn"]

    def test_group_add_task_without_shell_args_and_defensive_copy(self):
        group = Group(1)
        env = {"FOO": "1"}
        kwargs = {"b": 5}

        s1 = group.add_task(sync_fn, 3, **kwargs)
        s2 = group.add_command("echo", "hi", env=env)

        env["FOO"] = "mutated"
        kwargs["b"] = 999

        assert len(group) == 2
        assert list(group) == [s1, s2]
        assert s1.args == (3,)
        assert s1.kwargs == {"b": 5}
        assert s2.env == {"FOO": "1"}
        assert s2.shell_args == ("hi",)

    def test_group_add_command_rejects_non_string(self):
        group = Group(1)
        with pytest.raises(TypeError):
            group.add_command(123)  # type: ignore[arg-type]

    def test_workflow_groups_without_event_loop(self):
        wf = Workflow()
        g1 = wf.group()
        g2 = wf.group()
        assert len(wf) == 2
        assert wf.groups == [g1, g2]
        assert g1.id != g2.id
        assert wf.has_runner is False
