"""Secure Waitress startup on an operating-system-assigned loopback port."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from snla.ui.security import LoopbackSecurity, loopback_security

LOOPBACK_HOST = "127.0.0.1"


@dataclass(frozen=True)
class LaunchContext:
    """Public connection details for one local service launch."""

    origin: str
    bootstrap_url: str


def prepare_loopback_server(
    application: Any,
    *,
    security: LoopbackSecurity = loopback_security,
    server_factory: Any = None,
) -> tuple[Any, LaunchContext]:
    """Bind Waitress to a random loopback port and initialize launch auth."""

    if server_factory is None:
        from waitress.server import create_server

        server_factory = create_server

    server = server_factory(application, host=LOOPBACK_HOST, port=0, threads=4)
    effective_host = str(server.effective_host)
    effective_port = int(server.effective_port)
    if effective_host != LOOPBACK_HOST or not 1 <= effective_port <= 65535:
        server.close()
        raise RuntimeError("Waitress did not bind to a valid IPv4 loopback endpoint")

    origin = f"http://{LOOPBACK_HOST}:{effective_port}"
    bootstrap_token = security.begin_launch(origin)
    bootstrap_url = f"{origin}/#{urlencode({'bootstrap_token': bootstrap_token})}"
    return server, LaunchContext(origin=origin, bootstrap_url=bootstrap_url)


__all__ = ["LOOPBACK_HOST", "LaunchContext", "prepare_loopback_server"]
