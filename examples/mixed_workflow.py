import asyncio
from taskex import Env

from orchestral import Orchestral


async def fetch_data(source: str) -> str:
    await asyncio.sleep(0.05)
    return f"payload-from-{source}"


def summarize(items: list[str]) -> dict[str, int]:
    return {"count": len(items), "total_chars": sum(len(item) for item in items)}


async def main() -> None:
    config = Env(MERCURY_SYNC_EXECUTOR_TYPE="thread", MERCURY_SYNC_TASK_RUNNER_MAX_THREADS=4)

    async with Orchestral(config=config) as orch:
        with orch as workflow:
            # Group 1: shell + async tasks run concurrently
            ingest = workflow.group()
            ingest.add_command("echo", "starting ingest", alias="log_start")
            ingest.add_task(fetch_data, "api-a", alias="fetch_a")
            ingest.add_task(fetch_data, "api-b", alias="fetch_b")

            # Group 2: sync task runs after Group 1 completes
            process = workflow.group()
            process.add_task(summarize, ["alpha", "beta", "gamma"], alias="summarize")

        # Non-blocking run with live batch streaming
        await orch.run(wait=False, fail_fast=True, timeout="10s")

        async for batch in orch:
            print("Finished group:")
            for res in batch:
                print(f"  - {res.task_name}: {res.status.value} -> {res.result!r} ({res.elapsed:.3f}s)")

        print(f"Workflow {orch.status.value} in {orch.elapsed:.3f}s (error={orch.error})")


if __name__ == "__main__":
    asyncio.run(main())
