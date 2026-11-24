import asyncio

from orchestral import Orchestral


async def build() -> str:
    await asyncio.sleep(0.05)
    return "dist/app-1.0.0.whl"


async def unit_tests() -> str:
    await asyncio.sleep(0.05)
    return "42 passed"


async def lint() -> str:
    await asyncio.sleep(0.03)
    return "0 issues"


async def integration_tests() -> str:
    await asyncio.sleep(0.06)
    return "12 passed"


async def main() -> None:
    async with Orchestral() as orch:
        with orch as workflow:
            # Wave 1: build and lint run concurrently
            s_build = workflow.add_task(build)
            s_lint = workflow.add_task(lint)

            # Wave 2: unit_tests and integration_tests run concurrently after build
            s_unit = workflow.add_task(unit_tests, dependencies=[s_build])
            s_int = workflow.add_task(integration_tests, dependencies=["build"])

            # Wave 3: deploy runs only after lint, unit_tests, and integration_tests
            workflow.add_command(
                "echo",
                "deploying release",
                alias="deploy",
                dependencies=[s_lint, s_unit, s_int],
            )

        print("Execution waves:", orch.execution_order())

        await orch.run(wait=False, fail_fast=True, timeout="10s")
        async for wave_idx, batch in _enumerate_async(orch):
            print(f"Wave {wave_idx}:")
            for res in batch:
                print(f"  - {res.task_name}: {res.status.value} -> {res.result!r} ({res.elapsed:.3f}s)")

        print(f"Workflow {orch.status.value} in {orch.elapsed:.3f}s")


async def _enumerate_async(iterable):
    idx = 1
    async for item in iterable:
        yield idx, item
        idx += 1


if __name__ == "__main__":
    asyncio.run(main())
