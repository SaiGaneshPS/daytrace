"""FastAPI app factory.

DT-1 skeleton. Routers are added by their tickets (DT-11 onwards); DT-30 mounts the dashboard.
"""
from __future__ import annotations

from fastapi import FastAPI

from . import __version__


def create_app() -> FastAPI:
    return FastAPI(title="Daytrace hub", version=__version__)
