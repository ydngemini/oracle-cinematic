"""Sample the normalized component states every 2 s (one JSON line each)."""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, "/app")


async def main():
    from db import connection
    import component_health

    await connection.init_pool(min_size=1, max_size=2)
    end = time.time() + float(os.getenv("PROBE_SECONDS", "120"))
    while time.time() < end:
        t = time.time()
        snap = await component_health.snapshot(fresh=True)
        print(json.dumps({"wall": round(t, 1), **{n: c["state"] for n, c in snap["components"].items()}}), flush=True)
        await asyncio.sleep(max(0.0, 2.0 - (time.time() - t)))

asyncio.run(main())
