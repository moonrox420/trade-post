"""Drox Trade Post — top-level application entry point."""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import asynccontextmanager

import uvicorn

from .api import create_app
from .core.config import load_settings
from .core.logging_setup import configure_logging, get_logger
from .orchestrator.runtime import Orchestrator

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app):
    s = app.state.settings
    orch = Orchestrator(s)
    app.state.orchestrator = orch
    await orch.startup()
    log.info("Drox Trade Post ready on port %d", s.port)
    stop_event = asyncio.Event()

    def _on_signal(signame: str) -> None:
        log.info("Received %s, initiating shutdown", signame)
        stop_event.set()

    loop = asyncio.get_event_loop()
    for s_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s_name, _on_signal, s_name.name)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        yield
    finally:
        await orch.shutdown()
        log.info("Drox Trade Post stopped")


def main() -> None:
    s = load_settings()
    configure_logging(s)
    app = create_app(s)
    uvicorn.run(app, host=s.host, port=s.port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()

