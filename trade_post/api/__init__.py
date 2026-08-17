"""FastAPI HTTP/WS server: real auth, real data, no fakes."""

from .server import create_app

__all__ = ["create_app"]
