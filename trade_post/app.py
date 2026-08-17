"""Drox Trade Post — top-level application entry point."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn

# The `trade_post` package lives one directory above this file. This bootstrap
# makes both `python -m trade_post.app` (from the project root) and a plain
# `python app.py` (from inside the package directory) run unchanged.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trade_post.api import create_app
from trade_post.core.config import load_settings
from trade_post.core.logging_setup import configure_logging

# Project root, so relative paths (`.env`, default `trade_post.db`) resolve
# consistently regardless of the directory the process was launched from.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The FastAPI app's lifespan (owned by create_app) starts/stops the
# Orchestrator, runs migrations, and bootstraps the admin user. app.py only
# builds the app and hands control to the ASGI server.


def main() -> None:
    # Resolve relative config/DB paths (`.env`, default `trade_post.db`)
    # against the project root regardless of the launch directory.
    os.chdir(_PROJECT_ROOT)
    s = load_settings()
    configure_logging(s)
    app = create_app(s)
    # Print a clickable startup banner BEFORE uvicorn takes over stdout.
    scheme = "https" if s.app_env == "production" else "http"
    local_url = f"{scheme}://{s.public_host}:{s.port}"
    print()
    print("=" * 60)
    print(f"  {s.app_name}")
    print("=" * 60)
    print(f"  Open in browser: {local_url}")
    if s.host != s.public_host:
        # 0.0.0.0 (bind-all) is not a reachable browser address; show it
        # without a scheme so it is never mistaken for a URL.
        print(f"  Bound interface: {s.host}:{s.port}")
    print(f"  Mode:            {s.app_env}")
    print("  Login:           admin / <DROX_ADMIN_PASSWORD>")
    print("=" * 60)
    print()
    uvicorn.run(app, host=s.host, port=s.port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
