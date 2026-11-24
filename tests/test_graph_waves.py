import asyncio
import time

import pytest
from taskex.models import RunStatus, ShellProcess, TaskRun

from orchestral import (
    Orchestral,
    OrchestralCycleError,
    OrchestralGraphError,
    OrchestralTaskFailure,
    Step,
    Workflow,
    WorkflowStatus,
)


async def _noop() -> str:
    return "ok"


class TestGraphPlanningAndValidation:
    def test_diamond_dag_waves_and_out_of_order_declaration(self):
        wf = Workflow()
        # Declare downstream steps before upstream steps
        wf.add_task(_noop, alias="deploy", dependencies=["test_unit", "test_int"])
        wf.add_task(_noop, alias="test_unit", dependencies=["build"])
        wf.add_task(_noop, alias="test_int", dependencies=["build"])
        wf.add_task(_noop, alias="build")

        assert wf.execution_order() == [
            ["build"],
            ["test_unit", "test_int"],
            ["deploy"],
        ]

    def test_step_object_callable_and_string_dependencies(self):
        async def build():
            return "built"

        async def lint():
            return "linted"

        async def package():
            return "packaged"

        wf = Workflow()
        s_build = wf.add_task(build)
        s_lint = wf.add_task(lint)
        s_pkg = wf.add_task(package, dependencies=[s_build, lint])
        wf.add_command("echo", "done", alias="announce", dependencies="package")

        assert s_pkg.dependencies == ["build", "lint"]
        assert wf.execution_order() == [
            ["build", "lint"],
            ["package"],
            ["announce"],
        ]

    def test_step_direct_keyword_dependencies_and_deduplication(self):
        s = Step(_noop, "a", dependencies=["b", "a", "b"])
        assert s.dependencies == ["a", "b"]

    def test_invalid_dependency_types_rejected(self):
        with pytest.raises(TypeError):
            Step(_noop, dependencies=b"abc")  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            Step(_noop, dependencies=[123])  # type: ignore[list-item]
        with pytest.raises(ValueError):
            Step(_noop, dependencies=[""])

    def test_fan_in_duplicate_names_and_exact_step_identity(self):
        async def fetch(url: str) -> str:
            return url

        # Fan-in by function/name waits for ALL matching steps
        wf1 = Workflow()
        wf1.add_task(fetch, "a")
        wf1.add_task(fetch, "b")
        wf1.add_task(_noop, alias="merge", dependencies=[fetch])
        assert wf1.execution_order() == [["fetch", "fetch"], ["merge"]]

        # Dependency by Step object identity targets the exact Step instance
        wf2 = Workflow()
        s1 = wf2.add_task(fetch, "a")
        s2 = wf2.add_task(fetch, "b")
        wf2.add_task(_noop, alias="after_s1", dependencies=[s1])
        waves = wf2.build_waves()
        assert waves[0] == [s1, s2]
        assert [s.name for s in waves[1]] == ["after_s1"]

    def test_group_barriers_and_cross_group_dependencies(self):
        wf = Workflow()
        g1 = wf.group()
        g1.add_task(_noop, alias="init")
        g1.add_task(_noop, alias="prepare", dependencies=["init"])

        g2 = wf.group()
        g2.add_task(_noop, alias="run", dependencies=["prepare"])
        g2.add_task(_noop, alias="cleanup", dependencies=["run"])

        assert wf.execution_order() == [["init"], ["prepare"], ["run"], ["cleanup"]]
        assert g1.execution_order() == [["init"], ["prepare"]]

    def test_same_name_step_in_later_group_depends_on_earlier_group(self):
        wf = Workflow()
        g1 = wf.group()
        g1.add_command("echo", "v1", alias="stage")
        g2 = wf.group()
        g2.add_command("echo", "v2", alias="stage", dependencies=["stage"])

        assert wf.execution_order() == [["stage"], ["stage"]]

    def test_missing_dependency_raises_graph_error(self):
        wf = Workflow()
        wf.add_task(_noop, alias="a", dependencies=["does_not_exist"])

        with pytest.raises(OrchestralGraphError, match="unknown step 'does_not_exist'"):
            wf.build_waves()

    def test_external_step_object_not_in_workflow_raises_graph_error(self):
        orphan = Step(_noop, alias="orphan")
        wf = Workflow()
        wf.add_task(_noop, alias="a", dependencies=[orphan])

        with pytest.raises(OrchestralGraphError, match="not in this workflow"):
            wf.build_waves()

    def test_forward_cross_group_dependency_raises_graph_error(self):
        wf = Workflow()
        g1 = wf.group()
        g1.add_task(_noop, alias="early", dependencies=["late"])
        g2 = wf.group()
        g2.add_task(_noop, alias="late")

        with pytest.raises(OrchestralGraphError, match="later group"):
            wf.build_waves()

    def test_forward_cross_group_step_ref_raises_graph_error(self):
        wf = Workflow()
        g1 = wf.group()
        g2 = wf.group()
        late = g2.add_task(_noop, alias="late")
        g1.add_task(_noop, alias="early", dependencies=[late])

        with pytest.raises(OrchestralGraphError, match="later group"):
            wf.build_waves()

    def test_self_cycle_raises_cycle_error(self):
        wf = Workflow()
        wf.add_task(_noop, alias="a", dependencies=["a"])

        with pytest.raises(OrchestralCycleError) as exc_info:
            wf.build_waves()
        assert exc_info.value.cycle == ["a", "a"]

    def test_two_node_and_three_node_cycles_raise_cycle_error(self):
        wf2 = Workflow()
        wf2.add_task(_noop, alias="a", dependencies=["b"])
        wf2.add_task(_noop, alias="b", dependencies=["a"])

        with pytest.raises(OrchestralCycleError) as exc2:
            wf2.build_waves()
        assert exc2.value.cycle[0] == exc2.value.cycle[-1]
        assert set(exc2.value.cycle) == {"a", "b"}

        wf3 = Workflow()
        wf3.add_task(_noop, alias="root")
        wf3.add_task(_noop, alias="x", dependencies=["root", "z"])
        wf3.add_task(_noop, alias="y", dependencies=["x"])
        wf3.add_task(_noop, alias="z", dependencies=["y"])

        with pytest.raises(OrchestralCycleError) as exc3:
            wf3.build_waves()
        assert exc3.value.cycle[0] == exc3.value.cycle[-1]
        assert set(exc3.value.cycle) == {"x", "y", "z"}


@pytest.mark.asyncio
async def test_dag_executes_in_parallel_waves_and_streams_batches():
    events: list[str] = []

    async def work(name: str, delay: float) -> str:
        events.append(f"start:{name}")
        await asyncio.sleep(delay)
        events.append(f"end:{name}")
        return name

    async with Orchestral() as orch:
        with orch as wf:
            wf.add_task(work, "a", 0.02, alias="a")
            wf.add_task(work, "b", 0.08, alias="b", dependencies=["a"])
            wf.add_task(work, "c", 0.08, alias="c", dependencies=["a"])
            wf.add_task(work, "d", 0.01, alias="d", dependencies=["b", "c"])

        assert orch.execution_order() == [["a"], ["b", "c"], ["d"]]

        t0 = time.monotonic()
        await orch.run(wait=False)
        streamed = [[r.task_name for r in batch] async for batch in orch]
        elapsed = time.monotonic() - t0

        assert orch.status == WorkflowStatus.COMPLETED
        assert streamed == [["a"], ["b", "c"], ["d"]]
        # b and c ran concurrently (~0.11s total rather than ~0.19s sequential)
        assert elapsed < 0.16
        assert events.index("start:b") > events.index("end:a")
        assert events.index("start:c") > events.index("end:a")
        assert events.index("start:d") > events.index("end:b")
        assert events.index("start:d") > events.index("end:c")


@pytest.mark.asyncio
async def test_invalid_graph_raises_before_executing_any_step():
    ran = False

    async def side_effect():
        nonlocal ran
        ran = True

    async with Orchestral() as orch:
        with orch as wf:
            g1 = wf.group()
            g1.add_task(side_effect, alias="g1_step")
            g2 = wf.group()
            g2.add_task(_noop, alias="bad", dependencies=["missing"])

        with pytest.raises(OrchestralGraphError):
            await orch.run(wait=True)

        assert ran is False
        assert orch.status == WorkflowStatus.READY
        assert orch.results == {}


@pytest.mark.asyncio
async def test_failed_dependency_skips_downstream_branch_but_runs_independent_branch():
    executed: list[str] = []

    async def ok_step(name: str) -> str:
        executed.append(name)
        return name

    async def fail_step(name: str) -> str:
        executed.append(name)
        raise RuntimeError(f"{name} failed")

    async with Orchestral() as orch:
        with orch as wf:
            # Branch A: a1 fails -> a2 (callable) and a3 (shell) must be skipped
            wf.add_task(fail_step, "a1", alias="a1")
            wf.add_task(ok_step, "a2", alias="a2", dependencies=["a1"])
            wf.add_command("echo", "should_not_run", alias="a3", dependencies=["a2"])

            # Branch B: independent -> b1 and b2 must still complete
            wf.add_task(ok_step, "b1", alias="b1")
            wf.add_task(ok_step, "b2", alias="b2", dependencies=["b1"])

        await orch.run(wait=True, fail_fast=False)

        assert orch.status == WorkflowStatus.COMPLETED
        assert executed == ["a1", "b1", "b2"]

        a1_res = next(iter(orch.results["a1"].values()))
        a2_res = next(iter(orch.results["a2"].values()))
        a3_res = next(iter(orch.results["a3"].values()))
        b2_res = next(iter(orch.results["b2"].values()))

        assert a1_res.status == RunStatus.FAILED
        assert isinstance(a2_res, TaskRun)
        assert a2_res.status == RunStatus.CANCELLED
        assert "a1" in (a2_res.error or "")
        assert isinstance(a3_res, ShellProcess)
        assert a3_res.status == RunStatus.CANCELLED
        assert "a2" in (a3_res.error or "")
        assert b2_res.status == RunStatus.COMPLETE
        assert b2_res.result == "b2"


@pytest.mark.asyncio
async def test_cross_group_failed_dependency_skips_dependent_only():
    executed: list[str] = []

    async def fail_fn():
        executed.append("fail_fn")
        raise ValueError("boom")

    async def dependent_fn():
        executed.append("dependent_fn")
        return "dep"

    async def independent_fn():
        executed.append("independent_fn")
        return "indep"

    async with Orchestral() as orch:
        with orch as wf:
            g1 = wf.group()
            g1.add_task(fail_fn)

            g2 = wf.group()
            g2.add_task(dependent_fn, dependencies=[fail_fn])
            g2.add_task(independent_fn)

        await orch.run(wait=True, fail_fast=False)

        assert executed == ["fail_fn", "independent_fn"]
        assert next(iter(orch.results["dependent_fn"].values())).status == RunStatus.CANCELLED
        assert next(iter(orch.results["independent_fn"].values())).status == RunStatus.COMPLETE


@pytest.mark.asyncio
async def test_dag_fail_fast_halts_after_first_failed_wave():
    executed: list[str] = []

    async def fail_a():
        executed.append("a")
        raise RuntimeError("a broke")

    async def ok_b():
        executed.append("b")
        return "b"

    async with Orchestral() as orch:
        with orch as wf:
            wf.add_task(fail_a, alias="a")
            wf.add_task(ok_b, alias="b", dependencies=["a"])

        await orch.run(wait=True, fail_fast=True)

        assert orch.status == WorkflowStatus.FAILED
        assert isinstance(orch.error, OrchestralTaskFailure)
        assert executed == ["a"]
        assert "b" not in orch.results
