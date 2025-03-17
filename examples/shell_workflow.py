import asyncio

from orchestral import Orchestral


async def run():
    orch = Orchestral()
    with orch as workflow:
        group_one = workflow.group()

        group_one.add_command("ls")
        group_one.add_command("echo", "$PWD")

        group_two = workflow.group()
        group_two.add_command("cat", "test.py")

    await orch.run(wait=True)

    print(orch.results, "DONE!")

    await orch.shutdown()


asyncio.run(run())
