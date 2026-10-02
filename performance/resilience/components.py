"""Print the normalized component snapshot (run inside the perf network)."""
import asyncio
import json
import sys

sys.path.insert(0, "/app")


async def main():
    from db import connection
    import component_health

    await connection.init_pool(min_size=1, max_size=2)
    try:
        snap = await component_health.snapshot(fresh=True)
        print(json.dumps({n: [c["state"], c["summary"], c.get("detail")] for n, c in snap["components"].items()},
                         indent=1, default=str))
    finally:
        await connection.close_pool()

asyncio.run(main())
