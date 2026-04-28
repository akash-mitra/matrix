"""Matrix entrypoint: `uv run matrix` (or `python -m matrix`)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

import uvicorn

from matrix.channels.web import build_app
from matrix.core.harness import Harness


async def _run(host: str, port: int, agents_dir: Path) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    log = logging.getLogger("matrix")

    harness = Harness(agents_dir=agents_dir)
    harness.discover()
    await harness.start()

    app = build_app(harness)
    config = uvicorn.Config(
        app, host=host, port=port, log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    server_task = asyncio.create_task(server.serve(), name="uvicorn")
    log.info("matrix listening on http://%s:%d", host, port)

    await stop.wait()
    log.info("shutting down")
    server.should_exit = True
    await server_task
    await harness.stop()


def main() -> None:
    parser = argparse.ArgumentParser(prog="matrix")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--agents-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "agents",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.host, args.port, args.agents_dir))


if __name__ == "__main__":
    main()
