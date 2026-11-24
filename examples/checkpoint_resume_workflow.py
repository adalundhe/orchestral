import asyncio
import os
import tempfile

from orchestral import (
    Env,
    Orchestral,
    SQLiteCheckpointStore,
    checkpoint,
    last_checkpoint,
    pause_point,
)

FAIL_ONCE = True


async def fetch_source() -> list[str]:
    await asyncio.sleep(0.03)
    return ["alpha", "beta", "gamma", "delta"]


async def transform_records(items: list[str]) -> list[str]:
    prev = last_checkpoint()
    start_idx = prev.state["index"] if prev else 0
    processed = list(prev.state["processed"]) if prev else []

    for i in range(start_idx, len(items)):
        await pause_point()
        if FAIL_ONCE and i == 2:
            raise RuntimeError("Simulated outage at item index 2")
        processed.append(items[i].upper())
        await checkpoint({"index": i + 1, "processed": processed})
        await asyncio.sleep(0.02)

    return processed


async def main() -> None:
    global FAIL_ONCE

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "workflow_checkpoints.db")

        # 1. First run fails mid-way after checkpointing items 0 and 1
        cfg = Env(
            MERCURY_SYNC_EXECUTOR_TYPE="none",
            MERCURY_SYNC_CHECKPOINT_DB_PATH=db_path,
        )
        async with Orchestral(config=cfg, workflow_id="demo-etl") as orch1:
            with orch1 as wf:
                s1 = wf.add_task(fetch_source)
                s2 = wf.add_task(
                    transform_records,
                    ["alpha", "beta", "gamma", "delta"],
                    dependencies=[s1],
                )
                wf.add_command("echo", "pipeline complete", alias="notify", dependencies=[s2])

            await orch1.run(wait=True, fail_fast=True)
            cp = await orch1.get_checkpoint("transform_records")
            print("Run 1 status:", orch1.status.value, "| saved checkpoint:", cp.state if cp else None)

        # 2. Recover in a fresh Orchestral instance backed by the same SQLite DB
        FAIL_ONCE = False
        with SQLiteCheckpointStore(db_path) as store:
            async with Orchestral(
                config=Env(MERCURY_SYNC_EXECUTOR_TYPE="none"),
                checkpoint_store=store,
                workflow_id="demo-etl",
            ) as orch2:
                with orch2 as wf:
                    s1 = wf.add_task(fetch_source)
                    s2 = wf.add_task(
                        transform_records,
                        ["alpha", "beta", "gamma", "delta"],
                        dependencies=[s1],
                    )
                    wf.add_command("echo", "pipeline complete", alias="notify", dependencies=[s2])

                await orch2.resume(wait=False, fail_fast=True)
                await asyncio.sleep(0.01)
                await orch2.pause()
                print("Run 2 live status while paused:", orch2.status.value)
                await asyncio.sleep(0.05)
                await orch2.resume(wait=True)

                print("Run 2 final status:", orch2.status.value)
                res = next(iter(orch2.results["transform_records"].values()))
                print("Recovered result:", res.result, "| paused:", f"{orch2.paused_duration:.3f}s")


if __name__ == "__main__":
    asyncio.run(main())
