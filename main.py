import asyncio
import logging
import threading

import uvicorn

import config
from core.agent import Agent
from web.server import build_app

log = logging.getLogger("main")


async def _main() -> None:
    logging.basicConfig(level=config.LOG_LEVEL)
    agent = Agent()
    await agent.bootstrap()

    app = build_app(agent)
    uvicorn_config = uvicorn.Config(app, host="0.0.0.0", port=config.WEB_PORT, log_level=config.LOG_LEVEL.lower())
    server = uvicorn.Server(uvicorn_config)
    # Local web server always runs, independent of the platform connection.
    await server.serve()


if __name__ == "__main__":
    asyncio.run(_main())
